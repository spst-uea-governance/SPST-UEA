from pathlib import Path
import tomllib

import spst_runtime
from spst_runtime.benchmark import run_benchmark


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_version_is_consistent_across_metadata_and_evidence():
    project = tomllib.loads((REPOSITORY_ROOT / "runtime" / "pyproject.toml").read_text("utf-8"))

    assert project["project"]["version"] == "0.1.1"
    assert project["project"]["version"] == spst_runtime.__version__
    assert run_benchmark(dispatches=1)["runtime_version"] == spst_runtime.__version__


def test_public_repository_documents_are_substantive_not_placeholders():
    license_text = (REPOSITORY_ROOT / "LICENSE").read_text("utf-8")
    security_text = (REPOSITORY_ROOT / "SECURITY.md").read_text("utf-8")
    contributing_text = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text("utf-8")

    license_lines = license_text.splitlines()
    assert license_lines[0].strip() == "Apache License"
    assert license_lines[1].strip() == "Version 2.0, January 2004"
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "github.com/spst-uea-governance/SPST-UEA/security" in security_text
    assert "python -m pytest -q" in contributing_text
    assert len(security_text) >= 1_000
    assert len(contributing_text) >= 1_000
    for text in (license_text, security_text, contributing_text):
        assert "replace with the official" not in text.lower()


def test_governance_documents_are_strict_utf8_with_known_japanese_text():
    paths = [
        REPOSITORY_ROOT / "README.md",
        REPOSITORY_ROOT / "AGENTS.md",
        REPOSITORY_ROOT / "docs" / "spst-uea-covenant.md",
        REPOSITORY_ROOT / "docs" / "codex-autonomous-engineering-master-prompt-spst.md",
    ]
    decoded = {path: path.read_bytes().decode("utf-8") for path in paths}

    assert "改造ではなく、開花。シンプルで、美しく。" in decoded[REPOSITORY_ROOT / "README.md"]
    assert all("\ufffd" not in text for text in decoded.values())


def test_each_ci_workflow_executes_a_verification_command():
    workflows = sorted((REPOSITORY_ROOT / ".github" / "workflows").glob("*.yml"))

    assert workflows
    for workflow in workflows:
        text = workflow.read_text("utf-8")
        assert "run:" in text, f"{workflow.name} can report success without running verification"


def test_runtime_ci_enforces_measured_branch_coverage_without_skipping_db_guard():
    project = tomllib.loads((REPOSITORY_ROOT / "runtime" / "pyproject.toml").read_text("utf-8"))
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "runtime-ci.yml").read_text(
        "utf-8"
    )

    assert project["tool"]["coverage"]["run"]["branch"] is True
    assert project["tool"]["coverage"]["report"]["fail_under"] == 80
    assert "python -m coverage run -m pytest -q" in workflow
    assert "python -m coverage report" in workflow
    assert "Verify Live DB guard after full suite\n        if: always()" in workflow
