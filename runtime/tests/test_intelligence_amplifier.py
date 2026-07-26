from spst_runtime.intelligence_amplifier import IntelligenceAmplifier


def test_amplifier_classifies_and_scores_japanese_architecture_prompt():
    result = IntelligenceAmplifier().amplify(
        "知能増幅アーキテクチャを設計して実装する",
        {
            "turn_count": 3,
            "audit": [{"authorized": True}],
            "evaluation": {"esi": 1.0},
            "goals": ["maintain_api_key_free_codex_mediated_operation"],
            "prompts": ["previous"],
        },
    )

    assert result["intent"] == "implementation"
    assert "draft_architecture" in result["work_units"]
    assert "implement_minimal_safe_change" in result["work_units"]
    assert result["amplification_score"] == 1.0
    assert result["retrieved_context"][0].startswith("active_goal:")


def test_amplifier_flags_perfection_language_without_mojibake_tokens():
    result = IntelligenceAmplifier().amplify("完全生命体へ進めて下さい", {})

    assert "Does the answer avoid overstating actual model capability?" in result["reflection_checks"]


def test_amplifier_does_not_bypass_non_ready_context_packet_with_raw_memory():
    result = IntelligenceAmplifier().amplify(
        "verify mediated context",
        {
            "memory": {"retrieved": [{"text": "raw bypass must not appear"}]},
            "context_mediation": {"status": "blocked", "items": []},
        },
    )

    assert result["retrieved_context"] == []
    assert result["amplification_score"] == 0.7


def test_amplifier_uses_only_ready_mediated_items_when_packet_exists():
    result = IntelligenceAmplifier().amplify(
        "verify mediated context",
        {
            "memory": {"retrieved": [{"text": "raw bypass"}]},
            "context_mediation": {
                "status": "ready",
                "items": [{"text": "receipt-bound context"}],
            },
        },
    )

    assert result["retrieved_context"] == ["memory:receipt-bound context"]


def test_amplifier_preserves_legacy_runtime_memory_when_no_packet_exists():
    result = IntelligenceAmplifier().amplify(
        "verify legacy runtime context",
        {"memory": {"retrieved": [{"text": "legacy orchestrator context"}]}},
    )

    assert result["retrieved_context"] == ["memory:legacy orchestrator context"]
