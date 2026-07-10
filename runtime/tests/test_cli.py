import json

from spst_runtime.cli import main, run_dispatch, run_loop


def test_run_loop_returns_stopped_context():
    result = run_loop(2)

    assert result["running"] is False
    assert result["tick"] == 2
    assert result["history"] == ["tick:1", "tick:2"]


def test_run_dispatch_returns_pipeline_metadata():
    metadata = run_dispatch("test_event")

    assert metadata["last_event_type"] == "test_event"
    assert metadata["version"] == 1


def test_run_dispatch_can_route_prompt_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    metadata = run_dispatch("test_event", prompt="hello")

    assert metadata["model_inference"]["provider"] == "codex-mediated-local"
    assert metadata["model_inference"]["available"] is True
    assert metadata["model_inference"]["requires_api_key"] is False


def test_run_dispatch_can_route_gpt_prompt_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    metadata = run_dispatch("test_event", prompt="hello", use_gpt=True)

    assert metadata["model_inference"]["provider"] == "openai"
    assert metadata["model_inference"]["available"] is False


def test_main_prints_json(capsys):
    exit_code = main(["--steps", "1", "--event", "test_event"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["loop"]["tick"] == 1
    assert output["pipeline"]["last_event_type"] == "test_event"
