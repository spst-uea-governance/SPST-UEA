from spst_runtime.chat_bridge import get_chat_status, run_chat_turn


def test_run_chat_turn_uses_no_key_mode():
    result = run_chat_turn("hello", steps=1)

    assert result["mode"] == "codex-chat-mediated"
    assert result["requires_api_key"] is False
    assert result["runtime"]["loop"]["tick"] == 1
    assert result["runtime"]["pipeline"]["model_inference"]["provider"] == "codex-mediated-local"
    assert result["session"]["summary"]["status"] == "stable"
    assert result["session"]["amplification"]["amplification_score"] > 0
    assert result["session"]["memory"]["stats"]["total_records"] >= 1


def test_get_chat_status_has_session_shape():
    status = get_chat_status()

    assert status["requires_api_key"] is False
    assert "summary" in status
    assert "amplification" in status
    assert "memory" in status
    assert "turn_count" in status
