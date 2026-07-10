from spst_runtime.maintenance import run_maintenance


def test_run_maintenance_returns_expected_shape():
    result = run_maintenance()

    assert "memory_compaction" in result
    assert "generated_cleanup" in result
    assert "memory_stats" in result
