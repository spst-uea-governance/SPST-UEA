from pathlib import Path
import tomllib


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "runtime-ci.yml"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_runtime_ci_keeps_live_db_guard_around_import_and_full_suite():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    required = (
        "SPST_COCKPIT_DB_PATH",
        "Prepare Live DB hash guard",
        "Verify import isolation",
        "python -c \"import spst_runtime.web\"",
        "python -m coverage run -m pytest -q",
        "Verify Live DB guard after full suite",
        "if: always()",
        "Combine parent and subprocess coverage",
        "python -m coverage combine",
        "python -m coverage report",
        "python -m mypy spst_runtime",
        "git diff --check",
    )

    assert all(value in workflow for value in required)
    assert workflow.index("Prepare Live DB hash guard") < workflow.index(
        "Verify import isolation"
    )
    assert workflow.index("Verify import isolation") < workflow.index(
        "python -m coverage run -m pytest -q"
    )
    assert workflow.index("python -m coverage run -m pytest -q") < workflow.index(
        "Verify Live DB guard after full suite"
    )
    assert workflow.index("Verify Live DB guard after full suite") < workflow.index(
        "python -m coverage combine"
    )
    assert workflow.index("python -m coverage combine") < workflow.index(
        "python -m coverage report"
    )


def test_runtime_ci_resolves_runner_temp_during_step_execution():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "${{ runner.temp }}" not in workflow
    assert 'guard_dir="${RUNNER_TEMP}/spst-live-db-hash-guard"' in workflow
    assert '>> "$GITHUB_ENV"' in workflow


def test_runtime_ci_collects_subprocess_coverage_without_lowering_floor():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    run_config = config["tool"]["coverage"]["run"]
    report_config = config["tool"]["coverage"]["report"]

    assert run_config["branch"] is True
    assert run_config["parallel"] is True
    assert run_config["patch"] == ["subprocess"]
    assert report_config["fail_under"] == 80


def test_runtime_ci_direct_verification_toolchain_is_exactly_pinned():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev_dependencies = config["project"]["optional-dependencies"]["dev"]

    assert dev_dependencies == [
        "pytest==9.0.3",
        "pytest-asyncio==1.3.0",
        "coverage[toml]==7.13.5",
        "ruff==0.15.13",
        "mypy==1.20.1",
    ]

    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'python -m pip install -e "./runtime[dev]"' in workflow
    assert "python -m pip check" in workflow


def test_runtime_ci_actions_are_bound_to_immutable_commit_ids():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7" in workflow
    assert "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7" in workflow
    assert "actions/checkout@v" not in workflow
    assert "actions/setup-python@v" not in workflow
    assert "actions/setup-node@v" not in workflow


def test_runtime_ci_checks_committed_ranges_instead_of_the_clean_checkout():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required = (
        "fetch-depth: 0",
        "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}",
        "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}",
        "PUSH_BEFORE_SHA: ${{ github.event.before }}",
        'git diff --check "${PR_BASE_SHA}...${PR_HEAD_SHA}"',
        'git diff-tree --check --root --no-commit-id -r "$GITHUB_SHA"',
        'git diff --check "${PUSH_BEFORE_SHA}..${GITHUB_SHA}"',
    )
    assert all(item in workflow for item in required)
    assert "run: git diff --check\n" not in workflow
