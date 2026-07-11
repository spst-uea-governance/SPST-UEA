import sys


SNAPSHOT_PROFILE_NAMES = ("git_head", "git_status")
QUALITY_PROFILE_NAMES = ("pytest", "ruff", "mypy", "git_diff_check")
VERIFICATION_PROFILE_NAMES = (*SNAPSHOT_PROFILE_NAMES, *QUALITY_PROFILE_NAMES)


def command_for(profile: str) -> tuple[str, ...] | None:
    """Return the fixed, shell-free command for a local verification profile."""
    commands = {
        "git_head": ("git", "rev-parse", "HEAD"),
        "git_status": ("git", "status", "--short"),
        "git_diff_check": ("git", "diff", "--check"),
        "pytest": (sys.executable, "-m", "pytest", "-q"),
        "ruff": ("ruff", "check", "."),
        "mypy": (sys.executable, "-m", "mypy", "spst_runtime"),
    }
    return commands.get(profile)


def timeout_for(profile: str) -> int:
    """Return the bounded execution timeout for a fixed profile."""
    return 120 if profile == "pytest" else 60
