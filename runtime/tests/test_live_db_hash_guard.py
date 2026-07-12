import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_db_hash_guard.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "runtime"
    workspace.mkdir()
    database = tmp_path / "protected" / "spst_cockpit.db"
    manifest = tmp_path / "protected" / "manifest.json"
    result = _run(
        "prepare",
        "--db",
        str(database),
        "--manifest",
        str(manifest),
        "--workspace",
        str(workspace),
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return workspace, database, manifest


def test_live_db_hash_guard_accepts_unchanged_protected_database(tmp_path):
    _, database, manifest = _prepare(tmp_path)

    verified = _run("verify", "--manifest", str(manifest))
    payload = json.loads(verified.stdout)

    assert verified.returncode == 0
    assert payload["status"] == "passed"
    assert payload["database"]["sha256"]
    assert database.exists()


def test_live_db_hash_guard_rejects_hash_change(tmp_path):
    _, database, manifest = _prepare(tmp_path)
    database.write_bytes(database.read_bytes() + b"tampered")

    verified = _run("verify", "--manifest", str(manifest))
    payload = json.loads(verified.stdout)

    assert verified.returncode == 1
    assert payload["status"] == "failed"
    assert "live_db_hash_changed" in payload["reasons"]


def test_live_db_hash_guard_rejects_workspace_cockpit_artifact(tmp_path):
    workspace, _, manifest = _prepare(tmp_path)
    (workspace / "spst_cockpit.db").write_bytes(b"unexpected")

    verified = _run("verify", "--manifest", str(manifest))
    payload = json.loads(verified.stdout)

    assert verified.returncode == 1
    assert payload["status"] == "failed"
    assert "protected_workspace_artifact_created" in payload["reasons"]
