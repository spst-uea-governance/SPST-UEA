from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import spst_runtime.runtime_release_identity as release_identity_module
import spst_runtime.web as web
from spst_runtime.repository_identity import capture_repository_identity
from spst_runtime.runtime_release_identity import (
    RuntimeReleaseIdentity,
    capture_result,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPOSITORY_ROOT / "runtime" / "run_spst_web_8767.ps1"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, name: str = "repository") -> Path:
    repository = tmp_path / name
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "runtime@example.invalid")
    _git(repository, "config", "user.name", "Runtime Test")
    (repository / ".gitignore").write_text("*.db\nignored/\n", encoding="utf-8")
    (repository / "runtime.txt").write_text("initial\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")
    return repository


def test_runtime_release_identity_matches_exact_startup_repository(tmp_path: Path):
    repository = _repository(tmp_path)
    expected = capture_repository_identity(repository)
    release = RuntimeReleaseIdentity(repository)

    status = release.status()

    assert status["status"] == "ready"
    assert status["reason"] is None
    assert status["current_match"] is True
    assert status["startup_repository_identity"] == expected
    assert status["current_identity_sha256"] == expected["identity_sha256"]
    assert status["persistent_state_written"] is False
    assert status["path_disclosed"] is False


def test_runtime_release_identity_coalesces_only_overlapping_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(tmp_path)
    release = RuntimeReleaseIdentity(repository)
    original_compare = release_identity_module.compare_repository_identity
    capture_started = threading.Event()
    allow_capture = threading.Event()
    start_together = threading.Barrier(8)
    call_count = 0
    count_lock = threading.Lock()

    def slow_compare(expected: dict, root: Path) -> dict:
        nonlocal call_count
        with count_lock:
            call_count += 1
        capture_started.set()
        assert allow_capture.wait(timeout=5)
        return original_compare(expected, root)

    def concurrent_status() -> dict:
        start_together.wait(timeout=5)
        return release.status()

    monkeypatch.setattr(
        release_identity_module,
        "compare_repository_identity",
        slow_compare,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(concurrent_status) for _ in range(8)]
        assert capture_started.wait(timeout=5)
        time.sleep(0.05)
        allow_capture.set()
        results = [future.result(timeout=10) for future in futures]

    assert call_count == 1
    assert all(result["status"] == "ready" for result in results)
    assert len({result["current_identity_sha256"] for result in results}) == 1


def test_runtime_release_identity_does_not_cache_sequential_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(tmp_path)
    release = RuntimeReleaseIdentity(repository)
    original_compare = release_identity_module.compare_repository_identity
    call_count = 0

    def counting_compare(expected: dict, root: Path) -> dict:
        nonlocal call_count
        call_count += 1
        return original_compare(expected, root)

    monkeypatch.setattr(
        release_identity_module,
        "compare_repository_identity",
        counting_compare,
    )

    assert release.status()["status"] == "ready"
    assert release.status()["status"] == "ready"
    assert call_count == 2


def test_runtime_release_identity_status_copy_cannot_mutate_startup_binding(
    tmp_path: Path,
):
    repository = _repository(tmp_path)
    expected = capture_repository_identity(repository)
    release = RuntimeReleaseIdentity(repository)

    first = release.status()
    first["startup_repository_identity"]["head_revision"] = "0" * 40
    first["startup_repository_identity"]["identity_sha256"] = "0" * 64
    second = release.status()

    assert second["status"] == "ready"
    assert second["startup_repository_identity"] == expected


def test_runtime_release_identity_unexpected_compare_failure_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(tmp_path)
    release = RuntimeReleaseIdentity(repository)

    def fail_compare(expected: dict, root: Path) -> dict:
        raise RuntimeError("sensitive implementation detail")

    monkeypatch.setattr(
        release_identity_module,
        "compare_repository_identity",
        fail_compare,
    )

    result = release.status()

    assert result["status"] == "blocked"
    assert result["reason"] == "runtime_identity_status_failed:RuntimeError"
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("change", ["tracked", "untracked", "head"])
def test_runtime_release_identity_detects_repository_drift(
    tmp_path: Path,
    change: str,
):
    repository = _repository(tmp_path)
    release = RuntimeReleaseIdentity(repository)

    if change == "tracked":
        (repository / "runtime.txt").write_text("changed\n", encoding="utf-8")
    elif change == "untracked":
        (repository / "new.txt").write_text("new\n", encoding="utf-8")
    else:
        (repository / "runtime.txt").write_text("committed\n", encoding="utf-8")
        _git(repository, "add", "runtime.txt")
        _git(repository, "commit", "-qm", "change head")

    status = release.status()

    assert status["status"] == "stale"
    assert status["reason"] == "repository_identity_mismatch"
    assert status["current_match"] is False
    assert status["worktree_match"] is False
    assert status["head_match"] is (change != "head")


def test_runtime_release_identity_ignores_git_ignored_runtime_state(tmp_path: Path):
    repository = _repository(tmp_path)
    release = RuntimeReleaseIdentity(repository)
    ignored = repository / "ignored" / "cockpit.db"
    ignored.parent.mkdir()
    ignored.write_bytes(b"runtime state")

    status = release.status()

    assert status["status"] == "ready"
    assert status["current_match"] is True


def test_runtime_release_identity_blocks_when_current_capture_fails(tmp_path: Path):
    repository = _repository(tmp_path)
    release = RuntimeReleaseIdentity(repository)
    _git(repository, "worktree", "prune")
    (repository / ".git" / "HEAD").unlink()

    status = release.status()

    assert status["status"] == "blocked"
    assert status["current_match"] is False
    assert status["reason"].startswith("repository_identity_capture_failed:")


def test_capture_result_is_structured_for_non_repository(tmp_path: Path):
    result = capture_result(tmp_path)

    assert result["status"] == "blocked"
    assert result["repository_identity"] is None
    assert result["reason"].startswith("runtime_repository_identity_capture_failed:")


def test_runtime_identity_endpoint_does_not_initialize_cockpit_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(tmp_path)
    database = tmp_path / "must-not-exist.db"
    monkeypatch.setattr(web, "_DEFAULT_COCKPIT", None)
    monkeypatch.setattr(
        web,
        "_RUNTIME_RELEASE_IDENTITY",
        RuntimeReleaseIdentity(repository),
    )
    monkeypatch.setenv("SPST_COCKPIT_DB_PATH", str(database))

    status_code, result = web.handle_cockpit_request(
        "GET",
        "/api/runtime-identity",
    )

    assert status_code == HTTPStatus.OK
    assert result["status"] == "ready"
    assert web._DEFAULT_COCKPIT is None
    assert not database.exists()


def test_runtime_release_admission_allows_exact_current_repository(tmp_path: Path):
    repository = _repository(tmp_path)
    web.initialize_runtime_release_identity(repository)

    assert web.runtime_release_admission_failure() is None


@pytest.mark.parametrize("change", ["tracked", "untracked", "head"])
def test_runtime_release_admission_blocks_repository_drift(
    tmp_path: Path,
    change: str,
):
    repository = _repository(tmp_path)
    web.initialize_runtime_release_identity(repository)

    if change == "tracked":
        (repository / "runtime.txt").write_text("changed\n", encoding="utf-8")
    elif change == "untracked":
        (repository / "new.txt").write_text("new\n", encoding="utf-8")
    else:
        (repository / "runtime.txt").write_text("committed\n", encoding="utf-8")
        _git(repository, "add", "runtime.txt")
        _git(repository, "commit", "-qm", "change head")

    failure = web.runtime_release_admission_failure()

    assert failure is not None
    status_code, result = failure
    assert status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert result == {
        "schema": "spst-runtime-release-admission-v1",
        "status": "blocked",
        "reason": "runtime_release_identity_not_current",
        "identity_status": "stale",
        "identity_reason": "repository_identity_mismatch",
        "current_match": False,
        "path_disclosed": False,
        "persistent_state_written": False,
    }


def test_runtime_release_admission_blocks_uninitialized_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(web, "_RUNTIME_RELEASE_IDENTITY", None)

    failure = web.runtime_release_admission_failure()

    assert failure is not None
    status_code, result = failure
    assert status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert result["status"] == "blocked"
    assert result["identity_status"] == "blocked"
    assert result["identity_reason"] == "runtime_release_identity_not_initialized"


def test_runtime_release_admission_blocks_current_capture_failure(tmp_path: Path):
    repository = _repository(tmp_path)
    web.initialize_runtime_release_identity(repository)
    (repository / ".git" / "HEAD").unlink()

    failure = web.runtime_release_admission_failure()

    assert failure is not None
    status_code, result = failure
    assert status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert result["status"] == "blocked"
    assert result["identity_status"] == "blocked"
    assert result["identity_reason"].startswith(
        "repository_identity_capture_failed:"
    )
    assert result["current_match"] is False


@pytest.mark.parametrize(
    ("method", "path", "required"),
    [
        ("GET", "/api/run", True),
        ("GET", "/api/status", False),
        ("POST", "/api/dispatch", True),
        ("POST", "/api/project-context/query", True),
        ("POST", "/api/project-context/shutdown", False),
        ("POST", "/api/project-context/shutdown?reason=recovery", False),
        ("POST", "/api/project-context/shutdown/", True),
        ("POST", "/API/project-context/shutdown", True),
    ],
)
def test_runtime_release_admission_has_one_exact_recovery_post_exception(
    method: str,
    path: str,
    required: bool,
):
    assert web.runtime_release_admission_required(method, path) is required


def test_stale_http_runtime_blocks_execution_before_dispatch_or_body_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = _repository(tmp_path)
    web.initialize_runtime_release_identity(repository)
    (repository / "runtime.txt").write_text("changed\n", encoding="utf-8")

    def unexpected_run(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("stale runtime must not dispatch a chat turn")

    cockpit_calls: list[tuple[object, ...]] = []
    original_handle_cockpit_request = web.handle_cockpit_request

    def tracking_request(*args: object, **kwargs: object):
        cockpit_calls.append(args)
        if args[:2] == ("POST", "/api/project-context/shutdown"):
            return HTTPStatus.OK, {"status": "stopped"}
        return original_handle_cockpit_request(*args, **kwargs)

    monkeypatch.setattr(web, "run_chat_turn", unexpected_run)
    monkeypatch.setattr(web, "handle_cockpit_request", tracking_request)
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.RuntimeWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as get_error:
            urlopen(f"{base_url}/api/run?prompt=must-not-run", timeout=5)
        get_payload = json.loads(get_error.value.read().decode("utf-8"))

        request = Request(
            f"{base_url}/api/dispatch",
            data=b'{"event":"must-not-dispatch"}',
            method="POST",
        )
        with pytest.raises(HTTPError) as post_error:
            urlopen(request, timeout=5)
        post_payload = json.loads(post_error.value.read().decode("utf-8"))

        shutdown_request = Request(
            f"{base_url}/api/project-context/shutdown",
            data=b"{}",
            method="POST",
        )
        with urlopen(shutdown_request, timeout=5) as response:
            shutdown_payload = json.loads(response.read().decode("utf-8"))

        with urlopen(f"{base_url}/api/runtime-identity", timeout=5) as response:
            identity_payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/health", timeout=5) as response:
            health_payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert get_error.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    assert post_error.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    assert get_payload["reason"] == "runtime_release_identity_not_current"
    assert post_payload == get_payload
    assert shutdown_payload == {"status": "stopped"}
    assert identity_payload["status"] == "stale"
    assert health_payload == {"status": "ok"}
    assert cockpit_calls == [
        ("POST", "/api/project-context/shutdown"),
        ("GET", "/api/runtime-identity"),
    ]


def test_runtime_release_capture_cli_returns_exact_identity(tmp_path: Path):
    repository = _repository(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "spst_runtime.runtime_release_identity",
            "capture",
            "--repository-root",
            str(repository),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "ready"
    assert result["repository_identity"] == capture_repository_identity(repository)


def test_launcher_requires_current_runtime_and_requested_workspace_identity():
    script = LAUNCHER.read_text(encoding="utf-8")

    assert "spst_runtime.runtime_release_identity capture" in script
    assert "/api/runtime-identity" in script
    assert "Runtime release identity endpoint is unavailable" in script
    assert '$runtimeIdentity.status -ne "ready"' in script
    assert "-not $runtimeIdentity.current_match" in script
    assert (
        "$runtimeIdentity.startup_repository_identity.identity_sha256 "
        "-ne $expectedIdentity"
    ) in script
    assert "$runtimeIdentity.current_identity_sha256 -ne $expectedIdentity" in script
