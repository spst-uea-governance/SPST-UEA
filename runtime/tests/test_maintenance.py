from pathlib import Path

from pytest import MonkeyPatch

from spst_runtime import maintenance


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
