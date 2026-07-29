from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


STATUS_CONTEXT = "workflow-identity"
EXPECTED_BASE_REF = "master"
MAX_FILE_PAGES = 30
FILES_PER_PAGE = 100
PROTECTED_EXACT_PATHS = frozenset({"runtime/spst_runtime/workflow_identity_guard.py"})
PROTECTED_PREFIXES = (".github/workflows/",)
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}\Z")


class GuardError(RuntimeError):
    """A fail-closed workflow-identity evaluation error."""


class GuardClient(Protocol):
    def pull_request_identity(self, pull_request: int) -> tuple[str, str, str]: ...

    def list_pull_request_files(self, pull_request: int) -> list[dict[str, Any]]: ...

    def post_status(
        self,
        *,
        head_sha: str,
        state: str,
        description: str,
        target_url: str,
    ) -> None: ...


@dataclass(frozen=True)
class GuardEnvironment:
    repository: str
    pull_request: int
    head_sha: str
    base_ref: str
    base_sha: str
    target_url: str
    token: str
    api_url: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> GuardEnvironment:
        repository = values.get("SPST_REPOSITORY", "")
        pull_request_text = values.get("SPST_PR_NUMBER", "")
        head_sha = values.get("SPST_HEAD_SHA", "").lower()
        base_ref = values.get("SPST_BASE_REF", "")
        base_sha = values.get("SPST_BASE_SHA", "").lower()
        target_url = values.get("SPST_RUN_URL", "")
        token = values.get("GITHUB_TOKEN", "")
        api_url = values.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise GuardError("repository_identity_invalid")
        try:
            pull_request = int(pull_request_text)
        except ValueError as exc:
            raise GuardError("pull_request_identity_invalid") from exc
        if pull_request <= 0:
            raise GuardError("pull_request_identity_invalid")
        if not _OBJECT_ID_PATTERN.fullmatch(head_sha):
            raise GuardError("head_revision_invalid")
        if base_ref != EXPECTED_BASE_REF:
            raise GuardError("base_ref_invalid")
        if not _OBJECT_ID_PATTERN.fullmatch(base_sha):
            raise GuardError("base_revision_invalid")
        if not token:
            raise GuardError("github_token_missing")
        if not target_url.startswith("https://"):
            raise GuardError("run_url_invalid")
        if api_url != "https://api.github.com":
            raise GuardError("github_api_origin_invalid")
        return cls(
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=base_sha,
            target_url=target_url,
            token=token,
            api_url=api_url,
        )


class GitHubClient:
    def __init__(self, environment: GuardEnvironment, *, timeout_seconds: float = 15.0):
        self._environment = environment
        self._timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            f"{self._environment.api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._environment.token}",
                "User-Agent": "spst-workflow-identity-guard/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw = response.read()
        except HTTPError as exc:
            raise GuardError(f"github_api_http_{exc.code}") from exc
        except (TimeoutError, URLError) as exc:
            raise GuardError("github_api_unavailable") from exc
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuardError("github_api_response_invalid") from exc

    def list_pull_request_files(self, pull_request: int) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        repository = self._environment.repository
        for page in range(1, MAX_FILE_PAGES + 1):
            query = urlencode({"per_page": FILES_PER_PAGE, "page": page})
            payload = self._request(
                "GET", f"/repos/{repository}/pulls/{pull_request}/files?{query}"
            )
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise GuardError("pull_request_files_response_invalid")
            files.extend(payload)
            if len(payload) < FILES_PER_PAGE:
                return files
        raise GuardError("pull_request_files_pagination_unresolved")

    def pull_request_identity(self, pull_request: int) -> tuple[str, str, str]:
        payload = self._request(
            "GET", f"/repos/{self._environment.repository}/pulls/{pull_request}"
        )
        if not isinstance(payload, dict):
            raise GuardError("pull_request_identity_response_invalid")
        head = payload.get("head")
        base = payload.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GuardError("pull_request_identity_response_invalid")
        head_sha = head.get("sha")
        base_ref = base.get("ref")
        base_sha = base.get("sha")
        if not all(isinstance(value, str) for value in (head_sha, base_ref, base_sha)):
            raise GuardError("pull_request_identity_response_invalid")
        return str(head_sha).lower(), str(base_ref), str(base_sha).lower()

    def post_status(
        self,
        *,
        head_sha: str,
        state: str,
        description: str,
        target_url: str,
    ) -> None:
        self._request(
            "POST",
            f"/repos/{self._environment.repository}/statuses/{head_sha}",
            {
                "state": state,
                "context": STATUS_CONTEXT,
                "description": description,
                "target_url": target_url,
            },
        )


def _is_protected(path: str) -> bool:
    return path in PROTECTED_EXACT_PATHS or path.startswith(PROTECTED_PREFIXES)


def protected_changes(files: list[dict[str, Any]]) -> tuple[str, ...]:
    changed: set[str] = set()
    for item in files:
        filename = item.get("filename")
        previous_filename = item.get("previous_filename")
        if not isinstance(filename, str):
            raise GuardError("pull_request_filename_invalid")
        if previous_filename is not None and not isinstance(previous_filename, str):
            raise GuardError("pull_request_previous_filename_invalid")
        for path in (filename, previous_filename):
            if path is not None and _is_protected(path):
                changed.add(path)
    return tuple(sorted(changed))


def _emit(*, status: str, reason: str, changes: tuple[str, ...] = ()) -> None:
    print(
        json.dumps(
            {
                "schema": "spst-workflow-identity-guard-v1",
                "status": status,
                "reason": reason,
                "protected_changes": list(changes),
                "protected_change_count": len(changes),
                "status_context": STATUS_CONTEXT,
            },
            sort_keys=True,
        )
    )


def _require_current_pull_request_identity(
    environment: GuardEnvironment, client: GuardClient
) -> None:
    observed = client.pull_request_identity(environment.pull_request)
    expected = (environment.head_sha, environment.base_ref, environment.base_sha)
    if observed != expected:
        raise GuardError("pull_request_identity_changed")


def run_guard(environment: GuardEnvironment, client: GuardClient) -> int:
    client.post_status(
        head_sha=environment.head_sha,
        state="pending",
        description="Validating immutable workflow policy",
        target_url=environment.target_url,
    )
    try:
        _require_current_pull_request_identity(environment, client)
        changes = protected_changes(client.list_pull_request_files(environment.pull_request))
        if changes:
            client.post_status(
                head_sha=environment.head_sha,
                state="failure",
                description="Protected workflow policy changed",
                target_url=environment.target_url,
            )
            _emit(status="blocked", reason="protected_workflow_change", changes=changes)
            return 1
        _require_current_pull_request_identity(environment, client)
        client.post_status(
            head_sha=environment.head_sha,
            state="success",
            description="Protected workflow policy unchanged",
            target_url=environment.target_url,
        )
        _emit(status="accepted", reason="protected_workflow_unchanged")
        return 0
    except GuardError as exc:
        try:
            client.post_status(
                head_sha=environment.head_sha,
                state="error",
                description="Workflow identity unresolved; merge blocked",
                target_url=environment.target_url,
            )
        except GuardError:
            pass
        _emit(status="unresolved", reason=str(exc))
        return 2


def main() -> int:
    try:
        environment = GuardEnvironment.from_mapping(os.environ)
        return run_guard(environment, GitHubClient(environment))
    except GuardError as exc:
        _emit(status="unresolved", reason=str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
