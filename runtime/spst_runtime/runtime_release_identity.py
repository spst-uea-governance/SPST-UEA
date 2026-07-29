"""Fail-closed identity for the repository bytes loaded by a runtime process."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from threading import Condition
from typing import Any, Sequence

from spst_runtime.repository_identity import (
    RepositoryIdentityError,
    capture_repository_identity,
    compare_repository_identity,
)


RUNTIME_RELEASE_IDENTITY_SCHEMA = "spst-runtime-release-identity-v1"
RUNTIME_RELEASE_CAPTURE_SCHEMA = "spst-runtime-release-capture-v1"


class RuntimeReleaseIdentity:
    """Bind one process generation to its startup HEAD and worktree bytes."""

    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).expanduser().resolve()
        self._condition = Condition()
        self._capture_in_progress = False
        self._capture_generation = 0
        self._last_completed_status: dict[str, Any] | None = None
        self._startup_repository_identity: dict[str, Any] | None = None
        self._startup_error: str | None = None
        try:
            self._startup_repository_identity = capture_repository_identity(
                self.repository_root
            )
        except RepositoryIdentityError as error:
            self._startup_error = f"runtime_repository_identity_capture_failed:{error}"

    def status(self) -> dict[str, Any]:
        """Recapture current bytes and compare them with the process startup state."""

        with self._condition:
            startup = self._startup_repository_identity
            if startup is None:
                return self._blocked(self._startup_error or "runtime_identity_unavailable")
            if self._capture_in_progress:
                observed_generation = self._capture_generation
                self._condition.wait_for(
                    lambda: self._capture_generation > observed_generation
                )
                completed = self._last_completed_status
                if completed is None:
                    return self._blocked("runtime_identity_capture_result_unavailable")
                return deepcopy(completed)
            self._capture_in_progress = True

        result = self._capture_status(startup)
        with self._condition:
            self._last_completed_status = deepcopy(result)
            self._capture_generation += 1
            self._capture_in_progress = False
            self._condition.notify_all()
        return deepcopy(result)

    def _capture_status(self, startup: dict[str, Any]) -> dict[str, Any]:
        try:
            comparison = compare_repository_identity(startup, self.repository_root)
        except Exception as error:
            return self._blocked(
                f"runtime_identity_status_failed:{type(error).__name__}"
            )
        reason = comparison.get("reason")
        current_match = comparison.get("current_match")
        if current_match is True:
            status = "ready"
        elif isinstance(reason, str) and reason.startswith(
            "repository_identity_capture_failed:"
        ):
            status = "blocked"
        else:
            status = "stale"
        return {
            "schema": RUNTIME_RELEASE_IDENTITY_SCHEMA,
            "status": status,
            "reason": reason,
            "startup_repository_identity": startup,
            "current_match": current_match,
            "head_match": comparison.get("head_match"),
            "worktree_match": comparison.get("worktree_match"),
            "current_identity_sha256": comparison.get("current_identity_sha256"),
            "current_head_revision": comparison.get("current_head_revision"),
            "current_worktree_sha256": comparison.get("current_worktree_sha256"),
            "current_dirty": comparison.get("current_dirty"),
            "path_disclosed": False,
            "persistent_state_written": False,
        }

    def _blocked(self, reason: str) -> dict[str, Any]:
        return {
            "schema": RUNTIME_RELEASE_IDENTITY_SCHEMA,
            "status": "blocked",
            "reason": reason,
            "startup_repository_identity": None,
            "current_match": False,
            "head_match": None,
            "worktree_match": None,
            "current_identity_sha256": None,
            "current_head_revision": None,
            "current_worktree_sha256": None,
            "current_dirty": None,
            "path_disclosed": False,
            "persistent_state_written": False,
        }


def capture_result(repository_root: str | Path) -> dict[str, Any]:
    try:
        identity = capture_repository_identity(repository_root)
    except RepositoryIdentityError as error:
        return {
            "schema": RUNTIME_RELEASE_CAPTURE_SCHEMA,
            "status": "blocked",
            "reason": f"runtime_repository_identity_capture_failed:{error}",
            "repository_identity": None,
        }
    return {
        "schema": RUNTIME_RELEASE_CAPTURE_SCHEMA,
        "status": "ready",
        "reason": None,
        "repository_identity": identity,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--repository-root", required=True)
    args = parser.parse_args(argv)
    result = capture_result(args.repository_root)
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
