import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from spst_runtime.model_artifact_contract import build_model_artifact_contract
from spst_runtime.provider_observation import (
    CODEX_CLI_OBSERVATION_SOURCE,
    build_provider_request_binding,
    verify_provider_observation,
)
from spst_runtime.providers.codex_cli_adapter import (
    CODEX_CLI_BILLING_CLASS,
    CODEX_CLI_PROVIDER,
    CODEX_CLI_SPEND_GUARD,
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
        spend_guard_override: dict[str, object] | None = None,
    ):
        self.response_override = response_override
        self.thread_id = thread_id
        self.returncode = returncode
        self.event_override = event_override
        self.spend_guard_override = spend_guard_override
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.probe_calls: list[tuple[str, str, dict[str, str], int]] = []
        self.actions: list[str] = []
        self.schemas: list[dict[str, object]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((command, kwargs))
        if command[1:] == ["login", "status"]:
            self.actions.append("login")
            return subprocess.CompletedProcess(
                command,
                0,
                "Logged in using ChatGPT\n",
                "",
            )
        self.actions.append("exec")
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.schemas.append(schema)
        digest = schema["properties"]["request_binding_sha256"]["const"]
        response = self.response_override or {
            "request_binding_sha256": digest,
            "artifact": '{"answer":1}',
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

    def spend_guard_probe(
        self,
        executable: str,
        payload: str,
        environment: dict[str, str],
        timeout_seconds: int,
    ) -> str:
        self.actions.append("spend_guard")
        self.probe_calls.append(
            (executable, payload, environment, timeout_seconds)
        )
        rate_limits = self.spend_guard_override or {
            "limitId": "codex",
            "planType": "plus",
            "primary": {
                "usedPercent": 22,
                "windowDurationMins": 300,
                "resetsAt": 1785832476,
            },
            "secondary": None,
            "rateLimitReachedType": None,
            "credits": {
                "hasCredits": False,
                "unlimited": False,
                "balance": "0",
            },
        }
        return "\n".join(
            json.dumps(item)
            for item in (
                {"id": 0, "result": {"userAgent": "test"}},
                {
                    "id": 1,
                    "result": {
                        "account": {"type": "chatgpt", "planType": "plus"},
                        "requiresOpenaiAuth": True,
                    },
                },
                {"id": 2, "result": {"rateLimits": rate_limits}},
            )
        )


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
        spend_guard_probe=runner.spend_guard_probe,
    )
    context = {
        "instructions": "Return bounded JSON.",
        "evaluation": {"case_id": "real-01", "arm": "maximized"},
        "project_context_corpus": {"secret_marker": "verification-only-corpus"},
    }

    result = asyncio.run(adapter.infer("Solve the task.", context))

    assert result["provider"] == CODEX_CLI_PROVIDER
    assert result["model_version"] == "gpt-5.6-sol"
    assert result["text"] == '{"answer":1}'
    assert result["codex_cli"]["turn_completed"] is True
    assert result["codex_cli"]["zero_incremental_spend_guard"] == {
        "schema": CODEX_CLI_SPEND_GUARD,
        "account_type": "chatgpt",
        "plan_type": "plus",
        "included_usage_percent": 22,
        "maximum_included_usage_percent": 95,
        "spendable_credits_present": False,
        "credit_balance_zero": True,
        "rate_limit_reached": False,
        "source": "codex_app_server_account_rate_limits_read",
        "source_authenticated": False,
    }
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
    assert runner.actions == ["login", "spend_guard", "exec"]
    _, guard_payload, guard_environment, _ = runner.probe_calls[0]
    assert "account/rateLimits/read" in guard_payload
    assert "OPENAI_API_KEY" not in guard_environment
    assert "CODEX_API_KEY" not in guard_environment
    command, kwargs = runner.calls[1]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    provider_prompt = command[-1]
    assert '"canonical_model_input"' in provider_prompt
    assert '"instructions":"Return bounded JSON."' in provider_prompt
    assert '"adapter_context"' not in provider_prompt
    assert '"case_id"' not in provider_prompt
    assert "verification-only-corpus" not in provider_prompt
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment


def test_codex_cli_adapter_preserves_structured_task_artifact_as_canonical_json(
    tmp_path: Path,
):
    runner = StructuredRunner(
        response_override={
            "request_binding_sha256": "replaced-by-runner",
            "artifact": {"answer": "unknown"},
        }
    )

    def structured_runner(command: list[str], **kwargs: object):
        if command[1:] == ["login", "status"]:
            return runner(command, **kwargs)
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        runner.response_override = {
            "request_binding_sha256": schema["properties"][
                "request_binding_sha256"
            ]["const"],
            "artifact": {"answer": "unknown"},
        }
        return runner(command, **kwargs)

    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=structured_runner,
        spend_guard_probe=runner.spend_guard_probe,
    )
    context = {
        "artifact_contract": build_model_artifact_contract(
            ["answer"], {"answer": "string"}
        ),
        "evaluation": {"case_id": "structured-01"},
    }

    result = asyncio.run(adapter.infer("Return bounded JSON.", context))

    assert result["text"] == '{"answer":"unknown"}'
    assert runner.schemas[0]["properties"]["artifact"] == {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
        "additionalProperties": False,
    }
    assert "not an extracted inner value" in runner.calls[1][0][-1]


def test_codex_cli_adapter_rejects_bare_value_for_json_artifact_contract(
    tmp_path: Path,
):
    runner = StructuredRunner(
        response_override={
            "request_binding_sha256": "replaced-by-runner",
            "artifact": "unknown",
        }
    )

    def bare_value_runner(command: list[str], **kwargs: object):
        if command[1:] == ["login", "status"]:
            return runner(command, **kwargs)
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        runner.response_override = {
            "request_binding_sha256": schema["properties"][
                "request_binding_sha256"
            ]["const"],
            "artifact": "unknown",
        }
        return runner(command, **kwargs)

    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=bare_value_runner,
        spend_guard_probe=runner.spend_guard_probe,
    )
    context = {
        "artifact_contract": build_model_artifact_contract(
            ["answer"], {"answer": "string"}
        ),
        "evaluation": {"case_id": "structured-02"},
    }

    with pytest.raises(
        CodexCliAdapterError,
        match="codex_cli_provider_artifact_json_contract_mismatch",
    ):
        asyncio.run(adapter.infer("Return bounded JSON.", context))


def test_codex_cli_adapter_rejects_invalid_artifact_contract_before_any_probe(
    tmp_path: Path,
):
    runner = StructuredRunner()
    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=runner,
        spend_guard_probe=runner.spend_guard_probe,
    )

    with pytest.raises(
        CodexCliAdapterError,
        match="codex_cli_artifact_contract_schema_mismatch",
    ):
        asyncio.run(
            adapter.infer(
                "task",
                {"artifact_contract": {"schema": "altered"}},
            )
        )

    assert runner.actions == []


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
    assert capabilities["zero_incremental_spend_guard"] == CODEX_CLI_SPEND_GUARD
    assert capabilities["spendable_credits_must_be_absent"] is True
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
    runner = StructuredRunner(event_override=events)
    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=runner,
        spend_guard_probe=runner.spend_guard_probe,
    )
    with pytest.raises(CodexCliAdapterError, match=reason):
        asyncio.run(adapter.infer("task", {"evaluation": {}}))


def test_codex_cli_adapter_rejects_bad_ack_nonzero_exit_and_replay(tmp_path: Path):
    executable = str(_executable(tmp_path))
    mismatch_runner = StructuredRunner(
        response_override={
            "request_binding_sha256": "0" * 64,
            "artifact": "answer",
        }
    )
    mismatch = CodexCliAdapter(
        executable=executable,
        runner=mismatch_runner,
        spend_guard_probe=mismatch_runner.spend_guard_probe,
    )
    with pytest.raises(
        CodexCliAdapterError,
        match="codex_cli_request_acknowledgement_mismatch",
    ):
        asyncio.run(mismatch.infer("task", {"evaluation": {}}))

    failed_runner = StructuredRunner(returncode=3)
    failed = CodexCliAdapter(
        executable=executable,
        runner=failed_runner,
        spend_guard_probe=failed_runner.spend_guard_probe,
    )
    with pytest.raises(CodexCliAdapterError, match="codex_cli_nonzero_exit"):
        asyncio.run(failed.infer("task", {"evaluation": {}}))

    replay_runner = StructuredRunner(thread_id="replayed-thread")
    replay = CodexCliAdapter(
        executable=executable,
        runner=replay_runner,
        spend_guard_probe=replay_runner.spend_guard_probe,
    )
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


@pytest.mark.parametrize(
    ("rate_limits", "reason"),
    [
        (
            {
                "limitId": "codex",
                "planType": "plus",
                "primary": {"usedPercent": 22},
                "rateLimitReachedType": None,
                "credits": {"hasCredits": True, "unlimited": False, "balance": "1"},
            },
            "codex_cli_spendable_credits_present_or_unknown",
        ),
        (
            {
                "limitId": "codex",
                "planType": "plus",
                "primary": {"usedPercent": 22},
                "rateLimitReachedType": None,
                "credits": {"hasCredits": False, "unlimited": True, "balance": "0"},
            },
            "codex_cli_spendable_credits_present_or_unknown",
        ),
        (
            {
                "limitId": "codex",
                "planType": "plus",
                "primary": {"usedPercent": 22},
                "rateLimitReachedType": None,
            },
            "codex_cli_spendable_credits_present_or_unknown",
        ),
        (
            {
                "limitId": "codex",
                "planType": "plus",
                "primary": {"usedPercent": 95},
                "rateLimitReachedType": None,
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
            "codex_cli_included_plan_limit_unavailable",
        ),
        (
            {
                "limitId": "codex",
                "planType": "plus",
                "primary": {"usedPercent": 22},
                "rateLimitReachedType": "primary",
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
            "codex_cli_included_plan_limit_unavailable",
        ),
        (
            {
                "limitId": "codex",
                "planType": "business",
                "primary": {"usedPercent": 22},
                "rateLimitReachedType": None,
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
            "codex_cli_spend_guard_plan_mismatch",
        ),
    ],
)
def test_codex_cli_adapter_rejects_paid_or_unavailable_plan_usage_before_model_call(
    tmp_path: Path,
    rate_limits: dict[str, object],
    reason: str,
):
    runner = StructuredRunner(spend_guard_override=rate_limits)
    adapter = CodexCliAdapter(
        executable=str(_executable(tmp_path)),
        runner=runner,
        spend_guard_probe=runner.spend_guard_probe,
    )

    with pytest.raises(CodexCliAdapterError, match=reason):
        asyncio.run(adapter.infer("task", {"evaluation": {}}))

    assert runner.actions == ["login", "spend_guard"]
    assert [call[0][1] for call in runner.calls] == ["login"]
