from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from spst_runtime.workflow_identity_guard import (
    FILES_PER_PAGE,
    MAX_FILE_PAGES,
    GitHubClient,
    GuardEnvironment,
    GuardError,
    protected_changes,
    run_guard,
)


HEAD_SHA = "a" * 40
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "workflow-identity-guard.yml"


class FakeResponse:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class FakeClient:
    def __init__(
        self,
        files: list[dict[str, Any]] | None = None,
        *,
        listing_error: GuardError | None = None,
        observed_identity: tuple[str, str, str] | None = None,
    ):
        self.files = files or []
        self.listing_error = listing_error
        self.observed_identity = observed_identity or (HEAD_SHA, "master", "b" * 40)
        self.identity_reads = 0
        self.statuses: list[dict[str, str]] = []

    def pull_request_identity(self, pull_request: int) -> tuple[str, str, str]:
        assert pull_request == 17
        self.identity_reads += 1
        return self.observed_identity

    def list_pull_request_files(self, pull_request: int) -> list[dict[str, Any]]:
        assert pull_request == 17
        if self.listing_error is not None:
            raise self.listing_error
        return self.files

    def post_status(
        self,
        *,
        head_sha: str,
        state: str,
        description: str,
        target_url: str,
    ) -> None:
        assert head_sha == HEAD_SHA
        assert target_url == "https://github.com/example/project/actions/runs/99"
        self.statuses.append({"state": state, "description": description})


def environment(**overrides: str) -> GuardEnvironment:
    values = {
        "SPST_REPOSITORY": "example/project",
        "SPST_PR_NUMBER": "17",
        "SPST_HEAD_SHA": HEAD_SHA,
        "SPST_BASE_REF": "master",
        "SPST_BASE_SHA": "b" * 40,
        "SPST_RUN_URL": "https://github.com/example/project/actions/runs/99",
        "GITHUB_TOKEN": "test-token",
        "GITHUB_API_URL": "https://api.github.com",
    }
    values.update(overrides)
    return GuardEnvironment.from_mapping(values)


def test_guard_accepts_a_change_outside_the_protected_surface(capsys: pytest.CaptureFixture[str]):
    client = FakeClient([{"filename": "runtime/spst_runtime/web.py", "status": "modified"}])

    assert run_guard(environment(), client) == 0
    assert [status["state"] for status in client.statuses] == ["pending", "success"]
    assert client.identity_reads == 2
    assert '"status": "accepted"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "changed_file",
    [
        ".github/workflows/runtime-ci.yml",
        ".github/workflows/workflow-identity-guard.yml",
        ".github/workflows/new-spoof.yml",
        "runtime/spst_runtime/workflow_identity_guard.py",
    ],
)
def test_guard_blocks_every_workflow_or_guard_implementation_change(
    changed_file: str, capsys: pytest.CaptureFixture[str]
):
    client = FakeClient([{"filename": changed_file, "status": "modified"}])

    assert run_guard(environment(), client) == 1
    assert [status["state"] for status in client.statuses] == ["pending", "failure"]
    output = capsys.readouterr().out
    assert '"status": "blocked"' in output
    assert changed_file in output


def test_guard_blocks_a_rename_away_from_a_protected_path():
    files = [
        {
            "filename": "retired/runtime-ci.yml",
            "previous_filename": ".github/workflows/runtime-ci.yml",
            "status": "renamed",
        }
    ]

    assert protected_changes(files) == (".github/workflows/runtime-ci.yml",)


def test_guard_fails_closed_when_pull_request_files_are_unresolved(
    capsys: pytest.CaptureFixture[str],
):
    client = FakeClient(listing_error=GuardError("pull_request_files_pagination_unresolved"))

    assert run_guard(environment(), client) == 2
    assert [status["state"] for status in client.statuses] == ["pending", "error"]
    assert "pull_request_files_pagination_unresolved" in capsys.readouterr().out


@pytest.mark.parametrize(
    "observed_identity",
    [
        ("c" * 40, "master", "b" * 40),
        (HEAD_SHA, "release", "b" * 40),
        (HEAD_SHA, "master", "c" * 40),
    ],
)
def test_guard_rejects_a_pr_identity_that_changed_after_the_event(
    observed_identity: tuple[str, str, str], capsys: pytest.CaptureFixture[str]
):
    client = FakeClient(observed_identity=observed_identity)

    assert run_guard(environment(), client) == 2
    assert [status["state"] for status in client.statuses] == ["pending", "error"]
    assert "pull_request_identity_changed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"SPST_REPOSITORY": "not-a-repository"}, "repository_identity_invalid"),
        ({"SPST_PR_NUMBER": "0"}, "pull_request_identity_invalid"),
        ({"SPST_HEAD_SHA": "not-a-sha"}, "head_revision_invalid"),
        ({"SPST_BASE_REF": "release"}, "base_ref_invalid"),
        ({"SPST_BASE_SHA": "not-a-sha"}, "base_revision_invalid"),
        ({"GITHUB_TOKEN": ""}, "github_token_missing"),
        ({"SPST_RUN_URL": "http://example.test/run"}, "run_url_invalid"),
        ({"GITHUB_API_URL": "https://example.test"}, "github_api_origin_invalid"),
    ],
)
def test_guard_rejects_untrusted_environment_identity(
    override: dict[str, str], reason: str
):
    with pytest.raises(GuardError, match=reason):
        environment(**override)


def test_guard_rejects_malformed_file_records_instead_of_ignoring_them():
    with pytest.raises(GuardError, match="pull_request_filename_invalid"):
        protected_changes([{"filename": None}])

    with pytest.raises(GuardError, match="pull_request_previous_filename_invalid"):
        protected_changes([{"filename": "README.md", "previous_filename": 7}])


def test_github_client_reads_every_file_page_before_accepting():
    first_page = [{"filename": f"docs/{index}.md"} for index in range(FILES_PER_PAGE)]
    second_page = [{"filename": "README.md"}]
    client = GitHubClient(environment())

    with patch(
        "spst_runtime.workflow_identity_guard.urlopen",
        side_effect=[FakeResponse(first_page), FakeResponse(second_page)],
    ) as request:
        assert len(client.list_pull_request_files(17)) == FILES_PER_PAGE + 1

    assert "page=1" in request.call_args_list[0].args[0].full_url
    assert "page=2" in request.call_args_list[1].args[0].full_url


def test_github_client_recomputes_the_current_pr_identity():
    client = GitHubClient(environment())
    payload = {
        "head": {"sha": HEAD_SHA.upper()},
        "base": {"ref": "master", "sha": ("b" * 40).upper()},
    }

    with patch(
        "spst_runtime.workflow_identity_guard.urlopen", return_value=FakeResponse(payload)
    ):
        assert client.pull_request_identity(17) == (HEAD_SHA, "master", "b" * 40)


def test_github_client_fails_closed_at_the_api_pagination_boundary():
    full_page = [{"filename": f"docs/{index}.md"} for index in range(FILES_PER_PAGE)]
    client = GitHubClient(environment())

    with (
        patch(
            "spst_runtime.workflow_identity_guard.urlopen",
            side_effect=[FakeResponse(full_page) for _ in range(MAX_FILE_PAGES)],
        ),
        pytest.raises(GuardError, match="pull_request_files_pagination_unresolved"),
    ):
        client.list_pull_request_files(17)


def test_hosted_workflow_executes_only_the_trusted_base_guard_with_minimal_permissions():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required = (
        "pull_request_target:",
        "types: [opened, synchronize, reopened, ready_for_review, edited]",
        "contents: read",
        "pull-requests: read",
        "statuses: write",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "ref: ${{ github.event.pull_request.base.sha }}",
        "persist-credentials: false",
        "working-directory: runtime",
        "SPST_HEAD_SHA: ${{ github.event.pull_request.head.sha }}",
        "SPST_BASE_REF: ${{ github.event.pull_request.base.ref }}",
        "SPST_BASE_SHA: ${{ github.event.pull_request.base.sha }}",
        "python -m spst_runtime.workflow_identity_guard",
    )
    assert all(item in workflow for item in required)
    assert "actions/checkout@v" not in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in workflow
