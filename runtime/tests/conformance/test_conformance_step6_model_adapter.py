import asyncio

import pytest

from spst_runtime.providers.model_provider import ModelProvider


@pytest.mark.conformance
def test_conformance_step6_model_provider_falls_back_without_api_key():
    provider = ModelProvider()

    result = asyncio.run(provider.infer("hello", {"source": "conformance"}))

    assert result["available"] is True
    assert result["provider"] == "codex-mediated-local"
    assert result["requires_api_key"] is False
    assert result["context"] == {"source": "conformance"}


@pytest.mark.conformance
def test_conformance_step6_model_provider_exposes_vendor_neutral_health_and_capabilities():
    provider = ModelProvider()

    health = provider.health()
    capabilities = provider.get_capabilities()

    assert health["ok"] is True
    assert health["active_provider"] == "codex-mediated-local"
    assert health["requires_api_key"] is False
    assert capabilities["interface"] == "ModelAdapter"
    assert capabilities["methods"] == ["infer", "health", "get_capabilities"]
    assert "openai" not in capabilities["required_provider"]
