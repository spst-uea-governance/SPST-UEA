from spst_runtime.intelligence_amplifier import IntelligenceAmplifier


def test_amplifier_classifies_and_scores_architecture_prompt():
    result = IntelligenceAmplifier().amplify(
        "知能増幅アーキテクチャを設計し実装する",
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
    assert result["amplification_score"] == 1.0
    assert result["retrieved_context"][0].startswith("active_goal:")
