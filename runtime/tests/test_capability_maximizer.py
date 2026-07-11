from spst_runtime.engines.capability_maximizer import CapabilityMaximizer


def test_capability_maximizer_builds_safe_benchmark_scaffold():
    prompt = "Solve a coding benchmark task with hidden tests and strict correctness."

    result = CapabilityMaximizer().maximize(
        prompt,
        amplification={"intent": "implementation", "work_units": ["implement_minimal_safe_change"]},
        session_state={
            "memory": {"retrieved": [{"key": "rule", "text": "Prefer verified patches."}]}
        },
        payload={"benchmark_suite": "coding", "official_baseline_score": 80.0},
    )

    assert result["mode"] == "capability_maximization"
    assert result["target_domains"] == ["coding"]
    assert "spec_decomposition" in result["strategies"]
    assert "verifier_loop" in result["strategies"]
    assert result["verification_plan"]["requires_external_answer_leakage"] is False
    assert result["official_score_policy"]["model_weight_score_unchanged"] is True
    assert result["official_score_policy"]["reported_baseline_score"] == 80.0
    assert result["realized_performance"]["expected_direction"] == "increase"


def test_capability_maximizer_is_provider_neutral_and_deterministic():
    prompt = "Analyze a hard reasoning task and compare alternatives."
    maximizer = CapabilityMaximizer()

    first = maximizer.maximize(prompt, amplification={}, session_state={}, payload={})
    second = maximizer.maximize(prompt, amplification={}, session_state={}, payload={})

    assert first == second
    assert first["provider_policy"]["requires_specific_model"] is False
    assert first["provider_policy"]["api_key_required"] is False
    assert first["target_domains"] == ["reasoning"]
