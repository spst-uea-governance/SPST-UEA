import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SNAPSHOT_PROFILE_NAMES = ("git_head", "git_status")
QUALITY_PROFILE_NAMES = ("pytest", "ruff", "mypy", "git_diff_check")
VERIFICATION_PROFILE_NAMES = (*SNAPSHOT_PROFILE_NAMES, *QUALITY_PROFILE_NAMES)
PROFILE_CONTRACT_VERSION = 2


@dataclass(frozen=True)
class VerificationProfile:
    """A shell-free command and its repository-relative execution boundary."""

    command: tuple[str, ...]
    execution_root: str
    timeout_seconds: int
    required_paths: tuple[str, ...]


_PROFILES = {
    "git_head": VerificationProfile(
        command=("git", "rev-parse", "HEAD"),
        execution_root=".",
        timeout_seconds=60,
        required_paths=(".git",),
    ),
    "git_status": VerificationProfile(
        command=("git", "status", "--short"),
        execution_root=".",
        timeout_seconds=60,
        required_paths=(".git",),
    ),
    "git_diff_check": VerificationProfile(
        command=("git", "diff", "--check"),
        execution_root=".",
        timeout_seconds=60,
        required_paths=(".git",),
    ),
    "pytest": VerificationProfile(
        command=(sys.executable, "-m", "pytest", "-q"),
        execution_root="runtime",
        timeout_seconds=120,
        required_paths=("pyproject.toml", "spst_runtime"),
    ),
    "ruff": VerificationProfile(
        command=("ruff", "check", "."),
        execution_root="runtime",
        timeout_seconds=60,
        required_paths=("pyproject.toml", "spst_runtime"),
    ),
    "mypy": VerificationProfile(
        command=(sys.executable, "-m", "mypy", "spst_runtime"),
        execution_root="runtime",
        timeout_seconds=60,
        required_paths=("pyproject.toml", "spst_runtime"),
    ),
}


def profile_for(profile: str) -> VerificationProfile | None:
    """Return the immutable definition for a permitted profile."""
    return _PROFILES.get(profile)


def profile_contract_for(profile: str) -> dict[str, Any] | None:
    """Return the canonical profile contract bound into new action manifests."""
    definition = profile_for(profile)
    if definition is None:
        return None
    return {
        "version": PROFILE_CONTRACT_VERSION,
        "profile": profile,
        "command": list(definition.command),
        "execution_root": definition.execution_root,
        "timeout_seconds": definition.timeout_seconds,
        "required_paths": list(definition.required_paths),
    }


def resolve_execution_root(profile: str, repository_root: str | Path) -> tuple[Path, Path]:
    """Resolve a profile cwd from a validated repository boundary."""
    definition = profile_for(profile)
    if definition is None:
        raise ValueError("verification_profile_not_permitted")

    repository = Path(repository_root).expanduser().resolve()
    if not repository.is_dir() or not (repository / ".git").exists():
        raise ValueError("repository_root_invalid")

    relative = PurePosixPath(definition.execution_root)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("profile_execution_root_invalid")
    execution_root = (repository / Path(*relative.parts)).resolve()
    if execution_root != repository and repository not in execution_root.parents:
        raise ValueError("profile_execution_root_escapes_repository")
    if not execution_root.is_dir():
        raise ValueError("profile_execution_root_missing")
    if any(not (execution_root / marker).exists() for marker in definition.required_paths):
        raise ValueError("profile_execution_root_markers_missing")
    return repository, execution_root


def command_for(profile: str) -> tuple[str, ...] | None:
    """Return the fixed, shell-free command for a local verification profile."""
    definition = profile_for(profile)
    return definition.command if definition is not None else None


def timeout_for(profile: str) -> int:
    """Return the bounded execution timeout for a fixed profile."""
    definition = profile_for(profile)
    if definition is None:
        raise ValueError("verification_profile_not_permitted")
    return definition.timeout_seconds
