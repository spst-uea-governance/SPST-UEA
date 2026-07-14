from pathlib import Path

from spst_runtime.providers.tool_provider import ToolProvider


def test_dynamic_tool_generation_is_in_memory_and_preserves_package_source(tmp_path: Path):
    provider = ToolProvider(workspace_root=str(tmp_path))
    package_dynamic_dir = (
        Path(__file__).resolve().parents[1] / "spst_runtime" / "tools" / "dynamic"
    )
    before = sorted(path.name for path in package_dynamic_dir.glob("*.py"))

    result = provider.generate_dynamic_tool(
        "frontier_verifier",
        "unknown deterministic verifier",
    )

    after = sorted(path.name for path in package_dynamic_dir.glob("*.py"))
    assert result["status"] == "mounted"
    assert result["path"] is None
    assert result["artifact"]["storage"] == "in_memory"
    assert len(result["artifact"]["source_sha256"]) == 64
    assert result["tool_name"] in provider.list_tools()
    assert provider.run_dynamic_tool(result["tool_name"], {"prompt": "frontier"})[
        "status"
    ] == "solved"
    assert after == before
