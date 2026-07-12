from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "runtime-ci.yml"


def test_runtime_ci_keeps_live_db_guard_around_import_and_full_suite():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    required = (
        "SPST_COCKPIT_DB_PATH",
        "Prepare Live DB hash guard",
        "Verify import isolation",
        "python -c \"import spst_runtime.web\"",
        "python -m pytest -q",
        "Verify Live DB guard after full suite",
        "python -m mypy spst_runtime",
        "git diff --check",
    )

    assert all(value in workflow for value in required)
    assert workflow.index("Prepare Live DB hash guard") < workflow.index(
        "Verify import isolation"
    )
    assert workflow.index("Verify import isolation") < workflow.index(
        "python -m pytest -q"
    )
    assert workflow.index("python -m pytest -q") < workflow.index(
        "Verify Live DB guard after full suite"
    )
