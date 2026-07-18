from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


SCHEMA_VERSION = "codex-skill-discovery-probe-v1"
DEFAULT_SKILL_NAME = "aether-refine-code"
DEFAULT_PROMPT = "Please simplify and refactor existing code while preserving behavior."
AUTH_ENVIRONMENT_KEYS = (
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)
SKILL_ENTRY = re.compile(
    r"^- (?P<name>[a-z0-9-]+): (?P<description>.*?) "
    r"\(file: (?P<path>[^\r\n)]+/SKILL\.md)\)$",
    re.MULTILINE,
)


class DiscoveryError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_payload(path: Path, raw_payload: str) -> str:
    payload_bytes = raw_payload.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload_bytes)
    return sha256_bytes(payload_bytes)


def normalized_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/").rstrip("/")
    return normalized.casefold() if os.name == "nt" else normalized


def read_frontmatter(skill_path: Path) -> dict[str, str]:
    text = skill_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise DiscoveryError("skill_frontmatter_missing")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise DiscoveryError("skill_frontmatter_unterminated") from error
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        if ":" not in line:
            raise DiscoveryError("skill_frontmatter_invalid")
        key, value = line.split(":", maxsplit=1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def developer_texts(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise DiscoveryError("prompt_input_root_not_list")
    texts: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "input_text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def verify_discovery(
    payload: Any,
    repository_root: Path,
    skill_name: str = DEFAULT_SKILL_NAME,
) -> dict[str, Any]:
    repository_root = repository_root.resolve()
    skill_path = repository_root / ".agents" / "skills" / skill_name / "SKILL.md"
    if not skill_path.is_file():
        raise DiscoveryError("repository_skill_missing")

    texts = developer_texts(payload)
    skill_packets = [text for text in texts if "<skills_instructions>" in text]
    if len(skill_packets) != 1:
        raise DiscoveryError(f"skills_instruction_packet_count:{len(skill_packets)}")

    entries = [
        match.groupdict()
        for match in SKILL_ENTRY.finditer(skill_packets[0])
        if match.group("name") == skill_name
    ]
    if len(entries) != 1:
        raise DiscoveryError(f"skill_entry_count:{len(entries)}")

    entry = entries[0]
    expected_path = normalized_path(skill_path.resolve().as_posix())
    observed_path = normalized_path(entry["path"])
    if observed_path != expected_path:
        raise DiscoveryError("skill_locator_not_repository_bound")

    metadata = read_frontmatter(skill_path)
    if metadata.get("name") != skill_name:
        raise DiscoveryError("skill_frontmatter_name_mismatch")
    if entry["description"] != metadata.get("description"):
        raise DiscoveryError("skill_description_mismatch")

    skill_bytes = skill_path.read_bytes()
    return {
        "schema": SCHEMA_VERSION,
        "status": "passed",
        "skill": {
            "name": skill_name,
            "description": entry["description"],
            "locator": entry["path"],
            "sha256": sha256_bytes(skill_bytes),
        },
        "repository_root": str(repository_root),
        "skills_instruction_packets": len(skill_packets),
    }


def isolated_environment(isolation_root: Path) -> tuple[dict[str, str], Path, Path]:
    if isolation_root.exists():
        raise DiscoveryError("isolation_root_already_exists")
    codex_home = isolation_root / "codex-home"
    user_home = isolation_root / "user-home"
    codex_home.mkdir(parents=True)
    user_home.mkdir()
    environment = os.environ.copy()
    for key in AUTH_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "CODEX_NON_INTERACTIVE": "1",
            "HOME": str(user_home),
            "NO_COLOR": "1",
            "USERPROFILE": str(user_home),
            "XDG_CONFIG_HOME": str(isolation_root / "xdg-config"),
        }
    )
    return environment, codex_home, user_home


def run_command(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1_000:]
        raise DiscoveryError(f"fresh_codex_process_failed:{completed.returncode}:{detail}")
    return completed.stdout


def run_probe(
    codex: Path,
    repository_root: Path,
    isolation_root: Path,
    payload_output: Path,
    expected_cli_version: str,
    skill_name: str = DEFAULT_SKILL_NAME,
) -> dict[str, Any]:
    codex = codex.resolve()
    repository_root = repository_root.resolve()
    if not codex.is_file():
        raise DiscoveryError("codex_executable_missing")
    if not (repository_root / ".git").exists():
        raise DiscoveryError("repository_git_metadata_missing")

    environment, codex_home, user_home = isolated_environment(isolation_root.resolve())
    version_output = run_command([str(codex), "--version"], cwd=repository_root, environment=environment)
    version = version_output.strip()
    if version != f"codex-cli {expected_cli_version}":
        raise DiscoveryError(f"codex_cli_version_mismatch:{version}")

    raw_payload = run_command(
        [str(codex), "debug", "prompt-input", DEFAULT_PROMPT],
        cwd=repository_root,
        environment=environment,
    )
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as error:
        raise DiscoveryError("prompt_input_invalid_json") from error
    verification = verify_discovery(payload, repository_root, skill_name)

    payload_output = payload_output.resolve()
    prompt_input_sha256 = write_payload(payload_output, raw_payload)
    verification.update(
        {
            "auth_environment_removed": list(AUTH_ENVIRONMENT_KEYS),
            "codex_cli_version": expected_cli_version,
            "fresh_codex_home": str(codex_home),
            "fresh_user_home": str(user_home),
            "prompt_input_sha256": prompt_input_sha256,
        }
    )
    return verification


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--isolation-root", type=Path, required=True)
    parser.add_argument("--payload-output", type=Path, required=True)
    parser.add_argument("--expected-cli-version", required=True)
    parser.add_argument("--skill-name", default=DEFAULT_SKILL_NAME)
    args = parser.parse_args()
    try:
        result = run_probe(
            args.codex,
            args.repository_root,
            args.isolation_root,
            args.payload_output,
            args.expected_cli_version,
            args.skill_name,
        )
    except (DiscoveryError, OSError, subprocess.SubprocessError) as error:
        result = {
            "schema": SCHEMA_VERSION,
            "status": "failed",
            "reason": str(error),
        }
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
