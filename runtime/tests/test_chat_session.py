from spst_runtime.chat_session import evaluate_session, summarize_session


def test_evaluate_session_scores_stable_no_key_mode():
    result = evaluate_session(
        {
            "mode": "codex-chat-mediated",
            "provider": "codex-mediated-local",
            "goals": ["maintain_api_key_free_codex_mediated_operation"],
        }
    )

    assert result["esi"] == 1.0


def test_summarize_session_exposes_next_actions():
    summary = summarize_session(
        {
            "turn_count": 3,
            "prompts": ["a", "b", "c"],
            "audit": [{"authorized": True}],
            "evaluation": {"esi": 1.0},
        }
    )

    assert summary["status"] == "stable"
    assert summary["recent_prompt_count"] == 3
    assert summary["memory_count"] == 0
    assert "preserve_no_key_codex_mediated_boundary" in summary["next_actions"]
    assert "use_intelligence_amplification_scaffold" in summary["next_actions"]
