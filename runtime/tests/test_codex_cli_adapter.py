import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from spst_runtime.provider_observation import (
    CODEX_CLI_OBSERVATION_SOURCE,
    build_provider_request_binding,
    verify_provider_observation,
)
from spst_runtime.providers.codex_cli_adapter import (
    CODEX_CLI_BILLING_CLASS,
    CODEX_CLI_PROVIDER,
    CodexCliAdapter,
    CodexCliAdapterError,
)


class StructuredRunner:
    def __init__(
        self,
        *,
        response_override: dict[str, object] | None = None,
        thread_id: str = "thread-real-001",
        returncode: int = 0,
        event_override: list[dict[str, object]] | None = None,
    ):
        self.response_override = response_override
        self.thread_id = thread_id
        self.returncode = returncode
        self.event_override = event_override
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((command, kwargs))
        if command[1:] == ["login", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "Logged in using ChatGPT\n",
                "",
            )
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        digest = schema["properties"]["request_binding_sha256"]["const"]
        response = self.response_override or {
            "request_binding_sha256": digest,
            "answer": '{"answer":1}',
        }
        events = self.event_override or [
            {"type": "thread.started", "thread_id": self.thread_id},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "agent_message",
                    "text": json.dumps(response, sort_keys=True),
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
        stdout = "\n".join(json.dumps(event) for event in events)
        return subprocess.CompletedProcess(command, self.returncode, stdout, "")


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test executable placeholder")
    return executable


def test_codex_cli_adapter_emits_verified_observation_and_scrubs_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-propagate")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-propagate")
    runner = StructuredRunner()
    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        model="gpt-5.6-sol",
        runner=runner,
    )
    context = {
        "instructions": "Return bounded JSON.",
        "evaluation": {"case_id": "real-01", "arm": "maximized"},
    }

    result = asyncio.run(adapter.infer("Solve the task.", context))

    assert result["provider"] == CODEX_CLI_PROVIDER
    assert result["model_version"] == "gpt-5.6-sol"
    assert result["text"] == '{"answer":1}'
    assert result["codex_cli"]["turn_completed"] is True
    observation = result["provider_observation"]
    assert observation["observation_source"] == CODEX_CLI_OBSERVATION_SOURCE
    assert observation["provider"]["identity_cryptographically_verified"] is False
    assert observation["transport"]["source_authenticated"] is False
    request = build_provider_request_binding("Solve the task.", context)
    assert verify_provider_observation(
        observation,
        request,
        output_text=result["text"],
        provider_name=CODEX_CLI_PROVIDER,
        model_version="gpt-5.6-sol",
    ) == (True, None)
    assert runner.calls[0][0][1:] == ["login", "status"]
    command, kwargs = runner.calls[1]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment


def test_codex_cli_adapter_rejects_non_chatgpt_cached_auth_before_model_call(
    tmp_path: Path,
):
    calls: list[list[str]] = []

    def api_key_runner(command: list[str], **_: object):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "Logged in using an API key\n",
            "",
        )

    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=api_key_runner,
    )
    with pytest.raises(CodexCliAdapterError, match="codex_cli_chatgpt_auth_required"):
        asyncio.run(adapter.infer("task", {"evaluation": {}}))
    assert len(calls) == 1
    assert calls[0][1:] == ["login", "status"]


def test_codex_cli_adapter_declares_plan_usage_without_authentication(tmp_path: Path):
    adapter = CodexCliAdapter(executable=str(_executable(tmp_path)))
    health = adapter.health()
    capabilities = adapter.get_capabilities()

    assert health["ok"] is True
    assert health["authentication_mode"] == "chatgpt_cached_session_required"
    assert health["billing_state_cryptographically_verified"] is False
    assert health["provider_identity_cryptographically_verified"] is False
    assert capabilities["billing_class"] == CODEX_CLI_BILLING_CLASS
    assert capabilities["execution_environment"] == "external_network"
    assert capabilities["supports_transport_idempotency"] is False


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        ([{"type": "turn.completed", "usage": {}}], "codex_cli_thread_identity_missing"),
        (
            [{"type": "thread.started", "thread_id": "thread-1"}],
            "codex_cli_turn_incomplete",
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.failed"},
            ],
            "codex_cli_turn_failed",
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.completed", "usage": {}},
            ],
            "codex_cli_agent_message_missing",
        ),
    ],
)
def test_codex_cli_adapter_rejects_incomplete_event_streams(
    tmp_path: Path,
    events: list[dict[str, object]],
    reason: str,
):
    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=StructuredRunner(event_override=events),
    )
    with pytest.raises(CodexCliAdapterError, match=reason):
        asyncio.run(adapter.infer("task", {"evaluation": {}}))


def test_codex_cli_adapter_rejects_bad_ack_nonzero_exit_and_replay(tmp_path: Path):
    executable = str(_executable(tmp_path))
    mismatch = CodexCliAdapter(
        executable=executable,
        runner=StructuredRunner(
            response_override={
                "request_binding_sha256": "0" * 64,
                "answer": "answer",
            }
        ),
    )
    with pytest.raises(
        CodexCliAdapterError,
        match="codex_cli_request_acknowledgement_mismatch",
    ):
        asyncio.run(mismatch.infer("task", {"evaluation": {}}))

    failed = CodexCliAdapter(
        executable=executable,
        runner=StructuredRunner(returncode=3),
    )
    with pytest.raises(CodexCliAdapterError, match="codex_cli_nonzero_exit"):
        asyncio.run(failed.infer("task", {"evaluation": {}}))

    replay_runner = StructuredRunner(thread_id="replayed-thread")
    replay = CodexCliAdapter(executable=executable, runner=replay_runner)
    asyncio.run(replay.infer("task", {"evaluation": {}}))
    with pytest.raises(CodexCliAdapterError, match="codex_cli_response_replay_detected"):
        asyncio.run(replay.infer("task", {"evaluation": {}}))


def test_codex_cli_adapter_rejects_invalid_model_and_does_not_read_api_keys(
    tmp_path: Path,
):
    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        model="invalid model",
        runner=StructuredRunner(),
    )
    assert adapter.health()["ok"] is False
    with pytest.raises(CodexCliAdapterError, match="codex_cli_model_invalid"):
        asyncio.run(adapter.infer("task", {"evaluation": {}}))
    environment = CodexCliAdapter._chatgpt_only_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment
