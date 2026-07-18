from pathlib import Path

from pytest import MonkeyPatch

from spst_runtime import maintenance


def test_generated_cleanup_never_scans_or_deletes_repository_files(tmp_path: Path):
    cache_file = tmp_path / ".pytest_cache" / "user-owned.txt"
    cache_file.parent.mkdir()
    cache_file.write_text("preserve", encoding="utf-8")

    result = maintenance.clean_generated_files()

    assert result == {
        "status": "skipped",
        "reason": "repository_source_cleanup_disabled",
        "removed_files": 0,
        "removed_dirs": 0,
    }
    assert cache_file.read_text("utf-8") == "preserve"


def test_run_maintenance_returns_expected_shape(tmp_path: Path, monkeypatch: MonkeyPatch):
    monkeypatch.setattr(
        maintenance,
        "clean_generated_files",
        lambda: {"removed_files": 0, "removed_dirs": 0},
    )
    result = maintenance.run_maintenance(str(tmp_path / "memory.db"))

    assert "memory_compaction" in result
    assert "generated_cleanup" in result
    assert "memory_stats" in result
