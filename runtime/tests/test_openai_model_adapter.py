from spst_runtime.providers.openai_model_adapter import OpenAIModelAdapter


def test_openai_adapter_reports_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = OpenAIModelAdapter()

    assert adapter.available is False
