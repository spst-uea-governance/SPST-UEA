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
