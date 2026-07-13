import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any


REPOSITORY_IDENTITY_SCHEMA = "spst-repository-identity-v1"
WORKTREE_DIGEST_SCHEMA = b"spst-git-worktree-v1"


class RepositoryIdentityError(ValueError):
    """Reject repository identity claims that cannot be captured completely."""


def _canonical_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _run_git(root: Path, *arguments: str, reason: str) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise RepositoryIdentityError("git_unavailable") from error
    if completed.returncode != 0:
        raise RepositoryIdentityError(reason)
    return completed.stdout


def _frame(digest: Any, *values: bytes) -> None:
    digest.update(len(values).to_bytes(4, "big"))
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _parse_index(raw: bytes) -> list[tuple[bytes, bytes, bytes, bytes]]:
    records: list[tuple[bytes, bytes, bytes, bytes]] = []
    for raw_record in raw.split(b"\0"):
        if not raw_record:
            continue
        try:
            metadata, path = raw_record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
        except ValueError as error:
            raise RepositoryIdentityError("git_index_manifest_invalid") from error
        if not path or mode == b"160000":
            reason = "repository_submodules_unsupported" if mode == b"160000" else "git_path_invalid"
            raise RepositoryIdentityError(reason)
        records.append((path, stage, mode, object_id))
    return sorted(records)


def _parse_paths(raw: bytes) -> list[bytes]:
    paths = [path for path in raw.split(b"\0") if path]
    if len(paths) != len(set(paths)):
        raise RepositoryIdentityError("git_path_manifest_duplicated")
    return sorted(paths)


def _safe_candidate(root: Path, raw_path: bytes) -> Path:
    relative = Path(os.fsdecode(raw_path))
    if relative.is_absolute() or ".." in relative.parts:
        raise RepositoryIdentityError("git_path_outside_repository")
    candidate = root / relative
    try:
        candidate.parent.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise RepositoryIdentityError("git_path_outside_repository") from error
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _worktree_entry(root: Path, raw_path: bytes) -> tuple[bytes, bytes, bytes]:
    candidate = _safe_candidate(root, raw_path)
    try:
        before = candidate.lstat()
    except FileNotFoundError:
        return b"missing", b"-", b"-"

    executable = b"1" if before.st_mode & stat.S_IXUSR else b"0"
    if stat.S_ISLNK(before.st_mode):
        target = os.fsencode(os.readlink(candidate))
        content_digest = hashlib.sha256(target).hexdigest().encode("ascii")
        kind = b"symlink"
    elif stat.S_ISREG(before.st_mode):
        content_digest = _file_sha256(candidate).encode("ascii")
        kind = b"regular"
    else:
        raise RepositoryIdentityError("unsupported_worktree_entry_type")

    try:
        after = candidate.lstat()
    except FileNotFoundError as error:
        raise RepositoryIdentityError("repository_changed_during_capture") from error
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_mode,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_mode,
    ):
        raise RepositoryIdentityError("repository_changed_during_capture")
    return kind, executable, content_digest


def _digest_worktree(
    root: Path,
    index_records: list[tuple[bytes, bytes, bytes, bytes]],
    tracked_paths: list[bytes],
    untracked_paths: list[bytes],
) -> str:
    digest = hashlib.sha256()
    _frame(digest, WORKTREE_DIGEST_SCHEMA)
    for path, stage, mode, object_id in index_records:
        _frame(digest, b"index", path, stage, mode, object_id)
    for scope, paths in ((b"tracked", tracked_paths), (b"untracked", untracked_paths)):
        for path in paths:
            kind, executable, content_digest = _worktree_entry(root, path)
            _frame(digest, b"worktree", scope, path, kind, executable, content_digest)
    return digest.hexdigest()


def _resolve_repository_root(repository_root: str | Path) -> Path:
    try:
        root = Path(repository_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RepositoryIdentityError("repository_root_missing") from error
    if not root.is_dir():
        raise RepositoryIdentityError("repository_root_not_directory")
    raw_top_level = _run_git(
        root,
        "rev-parse",
        "--show-toplevel",
        reason="repository_not_git",
    ).rstrip(b"\r\n")
    try:
        top_level = Path(raw_top_level.decode("utf-8", errors="surrogateescape")).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RepositoryIdentityError("repository_toplevel_invalid") from error
    try:
        same_root = os.path.samefile(root, top_level)
    except OSError:
        same_root = os.path.normcase(str(root)) == os.path.normcase(str(top_level))
    if not same_root:
        raise RepositoryIdentityError("repository_root_not_toplevel")
    return root


def capture_repository_identity(repository_root: str | Path) -> dict[str, Any]:
    """Capture a path-free, read-only identity for one Git HEAD and worktree."""
    root = _resolve_repository_root(repository_root)
    head_revision = _run_git(
        root,
        "rev-parse",
        "--verify",
        "HEAD",
        reason="repository_head_unavailable",
    ).decode("ascii").strip().lower()
    object_format = _run_git(
        root,
        "rev-parse",
        "--show-object-format",
        reason="repository_object_format_unavailable",
    ).decode("ascii").strip().lower()
    expected_head_length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if expected_head_length == 0 or len(head_revision) != expected_head_length:
        raise RepositoryIdentityError("repository_head_invalid")
    try:
        int(head_revision, 16)
    except ValueError as error:
        raise RepositoryIdentityError("repository_head_invalid") from error

    index_before = _run_git(
        root,
        "ls-files",
        "--stage",
        "-z",
        reason="git_index_manifest_unavailable",
    )
    index_records = _parse_index(index_before)
    untracked_before = _run_git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        reason="git_untracked_manifest_unavailable",
    )
    untracked_paths = _parse_paths(untracked_before)
    tracked_paths = sorted({record[0] for record in index_records})
    if set(tracked_paths).intersection(untracked_paths):
        raise RepositoryIdentityError("git_path_scope_overlap")

    worktree_sha256 = _digest_worktree(
        root,
        index_records,
        tracked_paths,
        untracked_paths,
    )

    index_after = _run_git(
        root,
        "ls-files",
        "--stage",
        "-z",
        reason="git_index_manifest_unavailable",
    )
    untracked_after = _run_git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        reason="git_untracked_manifest_unavailable",
    )
    if index_before != index_after or untracked_before != untracked_after:
        raise RepositoryIdentityError("repository_changed_during_capture")
    if worktree_sha256 != _digest_worktree(
        root,
        index_records,
        tracked_paths,
        untracked_paths,
    ):
        raise RepositoryIdentityError("repository_changed_during_capture")

    status = _run_git(
        root,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        reason="git_status_unavailable",
    )
    identity = {
        "schema": REPOSITORY_IDENTITY_SCHEMA,
        "object_format": object_format,
        "head_revision": head_revision,
        "index_sha256": hashlib.sha256(index_before).hexdigest(),
        "worktree_sha256": worktree_sha256,
        "dirty": bool(status),
        "tracked_entry_count": len(tracked_paths),
        "untracked_entry_count": len(untracked_paths),
    }
    return {**identity, "identity_sha256": _canonical_hash(identity)}


def validate_repository_identity(identity: object) -> tuple[bool, str | None]:
    if not isinstance(identity, dict):
        return False, "repository_identity_missing"
    if identity.get("schema") != REPOSITORY_IDENTITY_SCHEMA:
        return False, "repository_identity_schema_mismatch"
    object_format = identity.get("object_format")
    head_revision = identity.get("head_revision")
    expected_head_length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if not isinstance(head_revision, str) or len(head_revision) != expected_head_length:
        return False, "repository_head_invalid"
    try:
        int(head_revision, 16)
    except ValueError:
        return False, "repository_head_invalid"
    if not _is_sha256(identity.get("index_sha256")) or not _is_sha256(
        identity.get("worktree_sha256")
    ):
        return False, "repository_worktree_digest_invalid"
    if type(identity.get("dirty")) is not bool:
        return False, "repository_dirty_state_invalid"
    for field in ("tracked_entry_count", "untracked_entry_count"):
        value = identity.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False, "repository_entry_count_invalid"
    identity_sha256 = identity.get("identity_sha256")
    if not _is_sha256(identity_sha256):
        return False, "repository_identity_digest_invalid"
    content = {key: value for key, value in identity.items() if key != "identity_sha256"}
    if identity_sha256 != _canonical_hash(content):
        return False, "repository_identity_digest_mismatch"
    return True, None


def compare_repository_identity(
    expected: dict[str, Any], repository_root: str | Path | None
) -> dict[str, Any]:
    valid, reason = validate_repository_identity(expected)
    if not valid:
        return {
            "bound": True,
            "binding_verified": False,
            "current_match": False,
            "reason": reason,
        }
    result: dict[str, Any] = {
        "bound": True,
        "binding_verified": True,
        "identity_sha256": expected["identity_sha256"],
        "head_revision": expected["head_revision"],
        "worktree_sha256": expected["worktree_sha256"],
        "dirty": expected["dirty"],
        "current_match": None,
        "reason": "repository_root_not_provided",
    }
    if repository_root is None:
        return result
    try:
        current = capture_repository_identity(repository_root)
    except RepositoryIdentityError as error:
        return {
            **result,
            "current_match": False,
            "reason": f"repository_identity_capture_failed:{error}",
        }
    head_match = expected["head_revision"] == current["head_revision"]
    worktree_match = expected["worktree_sha256"] == current["worktree_sha256"]
    identity_match = expected["identity_sha256"] == current["identity_sha256"]
    return {
        **result,
        "current_match": identity_match,
        "head_match": head_match,
        "worktree_match": worktree_match,
        "current_identity_sha256": current["identity_sha256"],
        "current_head_revision": current["head_revision"],
        "current_worktree_sha256": current["worktree_sha256"],
        "current_dirty": current["dirty"],
        "reason": None if identity_match else "repository_identity_mismatch",
    }
