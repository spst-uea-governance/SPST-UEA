import asyncio

from spst_runtime.providers.local_codex_adapter import LocalCodexAdapter


def test_local_codex_adapter_needs_no_api_key():
    result = asyncio.run(LocalCodexAdapter().infer("hello"))

    assert result["available"] is True
    assert result["requires_api_key"] is False
    assert result["prompt"] == "hello"
