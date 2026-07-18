from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "runtime" / "scripts" / "codex_skill_discovery_probe.py"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "runtime-ci.yml"
SPEC = importlib.util.spec_from_file_location("codex_skill_discovery_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    skill = root / ".agents" / "skills" / "aether-refine-code" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    description = "Refine code safely. Use for bounded refactoring tasks."
    skill.write_text(
        f"---\nname: aether-refine-code\ndescription: {description}\n---\n\n# Contract\n",
        encoding="utf-8",
    )
    return root, description


def _payload(path: Path, description: str, *, copies: int = 1) -> list[dict]:
    entry = f"- aether-refine-code: {description} (file: {path.as_posix()})"
    packet = "<skills_instructions>\n### Available skills\n" + "\n".join(
        entry for _ in range(copies)
    )
    return [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": packet}],
        }
    ]


def test_verifier_accepts_one_repository_bound_skill(tmp_path: Path) -> None:
    root, description = _repository(tmp_path)
    skill = root / ".agents" / "skills" / "aether-refine-code" / "SKILL.md"

    result = PROBE.verify_discovery(_payload(skill, description), root)

    assert result["status"] == "passed"
    assert result["skill"]["name"] == "aether-refine-code"
    assert result["skill"]["locator"] == skill.as_posix()


@pytest.mark.parametrize(
    ("payload_factory", "reason"),
    [
        (lambda skill, description: _payload(skill, description, copies=0), "skill_entry_count:0"),
        (lambda skill, description: _payload(skill, description, copies=2), "skill_entry_count:2"),
        (
            lambda skill, description: _payload(skill.parent / "other" / "SKILL.md", description),
            "skill_locator_not_repository_bound",
        ),
        (
            lambda skill, description: _payload(skill, description + " altered"),
            "skill_description_mismatch",
        ),
    ],
)
def test_verifier_fails_closed_on_unbound_discovery(
    tmp_path: Path,
    payload_factory,
    reason: str,
) -> None:
    root, description = _repository(tmp_path)
    skill = root / ".agents" / "skills" / "aether-refine-code" / "SKILL.md"

    with pytest.raises(PROBE.DiscoveryError, match=reason):
        PROBE.verify_discovery(payload_factory(skill, description), root)


def test_isolated_environment_removes_paid_auth_and_rejects_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in PROBE.AUTH_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "must-not-reach-fresh-process")
    isolation_root = tmp_path / "isolation"

    environment, codex_home, user_home = PROBE.isolated_environment(isolation_root)

    assert all(key not in environment for key in PROBE.AUTH_ENVIRONMENT_KEYS)
    assert environment["CODEX_HOME"] == str(codex_home)
    assert environment["HOME"] == str(user_home)
    assert codex_home.is_dir()
    assert user_home.is_dir()
    with pytest.raises(PROBE.DiscoveryError, match="isolation_root_already_exists"):
        PROBE.isolated_environment(isolation_root)


def test_payload_evidence_hashes_exact_written_bytes(tmp_path: Path) -> None:
    output = tmp_path / "prompt-input.json"
    raw_payload = '[{"line":"one\\ntwo"}]\n'

    digest = PROBE.write_payload(output, raw_payload)

    assert output.read_bytes() == raw_payload.encode("utf-8")
    assert digest == PROBE.sha256_bytes(output.read_bytes())


def test_runtime_ci_runs_pinned_keyless_fresh_process_discovery() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required = (
        'CODEX_CLI_VERSION: "0.144.5"',
        "actions/setup-node@v4",
        "Install pinned Codex CLI for discovery probe",
        "@openai/codex@${CODEX_CLI_VERSION}",
        "Verify fresh Codex process discovers repository skill",
        "codex_skill_discovery_probe.py",
        '--expected-cli-version "$CODEX_CLI_VERSION"',
    )
    assert all(value in workflow for value in required)
    assert "CODEX_API_KEY" not in workflow
    assert "CODEX_ACCESS_TOKEN" not in workflow
    assert workflow.index("Install pinned Codex CLI for discovery probe") < workflow.index(
        "Verify fresh Codex process discovers repository skill"
    )
