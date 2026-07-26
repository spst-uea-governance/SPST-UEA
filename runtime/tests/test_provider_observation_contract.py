from copy import deepcopy

import pytest

from spst_runtime.live_pairing import (
    LivePairingError,
    build_live_pair_execution,
    build_live_pair_plan,
    canonical_live_pair_hash,
    validate_live_pair_execution,
    validate_live_pair_execution_set,
    validate_live_pair_plan,
)
from spst_runtime.provider_observation import (
    ProviderObservationError,
    build_provider_observation,
    build_provider_request_binding,
    canonical_provider_value_sha256,
    validate_provider_observation_binding,
    validate_provider_request_binding,
    verify_provider_observation,
)


SHA = "1" * 64


def _resign(value: dict, digest_field: str) -> dict:
    updated = deepcopy(value)
    unsigned = {key: item for key, item in updated.items() if key != digest_field}
    updated[digest_field] = canonical_provider_value_sha256(unsigned)
    return updated


def _resign_live(value: dict) -> dict:
    updated = deepcopy(value)
    unsigned = {
        key: item
        for key, item in updated.items()
        if key != "execution_binding_sha256"
    }
    updated["execution_binding_sha256"] = canonical_live_pair_hash(unsigned)
    return updated


def _observation(prompt: str = "bound") -> tuple[dict, dict]:
    request = build_provider_request_binding(prompt, {"evaluation": {"case": "one"}})
    observation = build_provider_observation(
        request,
        provider_name="observable-provider",
        model_version="observable-v1",
        response_id="response-one",
        response_status="completed",
        output_text='{"answer": 1}',
        observation_source="in_process_provider_echo",
        acknowledged_request_binding_sha256=request["request_binding_sha256"],
    )
    return request, observation


def test_provider_request_binding_rejects_invalid_and_resigned_fields(monkeypatch):
    with pytest.raises(ProviderObservationError, match="provider_request_context_invalid"):
        build_provider_request_binding("prompt", ["not", "a", "mapping"])
    with pytest.raises(
        ProviderObservationError,
        match="context_intervention",
    ):
        build_provider_request_binding(
            "prompt",
            {"evidence_context_intervention": {"invalid": True}},
        )
    with pytest.raises(
        ProviderObservationError,
        match="provider_request_evaluation_context_invalid",
    ):
        build_provider_request_binding("prompt", {"evaluation": "invalid"})

    request, _ = _observation()
    monkeypatch.setattr(
        "spst_runtime.provider_observation.bind_model_input",
        lambda prompt, context: {"delivered_context_items": True},
    )
    with pytest.raises(
        ProviderObservationError,
        match="provider_request_model_input_invalid",
    ):
        build_provider_request_binding("prompt")
    assert validate_provider_request_binding(None) == (
        False,
        "provider_request_binding_invalid",
    )
    altered = deepcopy(request)
    altered["request_binding_sha256"] = "0" * 64
    assert validate_provider_request_binding(altered)[1] == (
        "provider_request_binding_digest_mismatch"
    )

    invalid_field = deepcopy(request)
    invalid_field["prompt_sha256"] = "invalid"
    invalid_field = _resign(invalid_field, "request_binding_sha256")
    assert validate_provider_request_binding(invalid_field)[1] == (
        "provider_request_binding_field_invalid"
    )
    invalid_optional = deepcopy(request)
    invalid_optional["context_packet_sha256"] = "invalid"
    invalid_optional = _resign(invalid_optional, "request_binding_sha256")
    assert validate_provider_request_binding(invalid_optional)[1] == (
        "provider_request_binding_context_invalid"
    )
    invalid_boolean = deepcopy(request)
    invalid_boolean["context_present"] = "false"
    invalid_boolean = _resign(invalid_boolean, "request_binding_sha256")
    assert validate_provider_request_binding(invalid_boolean)[1] == (
        "provider_request_binding_context_invalid"
    )
    inconsistent_presence = deepcopy(request)
    inconsistent_presence["context_binding_sha256"] = SHA
    inconsistent_presence = _resign(inconsistent_presence, "request_binding_sha256")
    assert validate_provider_request_binding(inconsistent_presence)[1] == (
        "provider_request_binding_context_invalid"
    )
    missing_combined_binding = deepcopy(request)
    missing_combined_binding["context_intervention_sha256"] = SHA
    missing_combined_binding = _resign(
        missing_combined_binding,
        "request_binding_sha256",
    )
    assert validate_provider_request_binding(missing_combined_binding)[1] == (
        "provider_request_binding_context_invalid"
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"provider_name": "bad provider"}, "provider_observation_provider_invalid"),
        ({"model_version": "bad model"}, "provider_observation_model_invalid"),
        ({"response_id": ""}, "provider_observation_response_id_missing"),
        ({"response_status": "failed"}, "provider_observation_response_incomplete"),
        ({"observation_source": "caller_claim"}, "provider_observation_source_invalid"),
        (
            {"acknowledged_request_binding_sha256": "0" * 64},
            "provider_request_acknowledgement_mismatch",
        ),
    ],
)
def test_provider_observation_builder_records_unresolved_reasons(overrides, reason):
    request, _ = _observation()
    arguments = {
        "provider_name": "observable-provider",
        "model_version": "observable-v1",
        "response_id": "response-two",
        "response_status": "completed",
        "output_text": '{}',
        "observation_source": "in_process_provider_echo",
        "acknowledged_request_binding_sha256": request["request_binding_sha256"],
        **overrides,
    }
    observation = build_provider_observation(request, **arguments)
    assert observation["status"] == "unresolved"
    assert observation["reason"] == reason

    invalid_request = deepcopy(request)
    invalid_request["request_binding_sha256"] = "0" * 64
    with pytest.raises(ProviderObservationError, match="digest_mismatch"):
        build_provider_observation(invalid_request, **arguments)


def test_provider_observation_verifier_rejects_boundary_mismatches():
    request, observation = _observation()
    assert verify_provider_observation(
        observation,
        request,
        output_text='{"answer": 1}',
        provider_name="observable-provider",
        model_version="observable-v1",
    ) == (True, None)
    assert verify_provider_observation(None, request, output_text="")[1] == (
        "provider_observation_missing"
    )

    invalid_expected = deepcopy(request)
    invalid_expected["request_binding_sha256"] = "0" * 64
    assert verify_provider_observation(
        observation,
        invalid_expected,
        output_text='{"answer": 1}',
    )[1] == "provider_request_binding_digest_mismatch"
    other_request = build_provider_request_binding("other")
    assert verify_provider_observation(
        observation,
        other_request,
        output_text='{"answer": 1}',
    )[1] == "provider_observation_request_mismatch"
    assert verify_provider_observation(
        observation,
        request,
        output_text='{"answer": 1}',
        provider_name="other-provider",
    )[1] == "provider_observation_provider_mismatch"
    assert verify_provider_observation(
        observation,
        request,
        output_text='{"answer": 1}',
        model_version="other-model",
    )[1] == "provider_observation_model_mismatch"
    assert verify_provider_observation(
        observation,
        request,
        output_text='{"answer": 0}',
    )[1] == "provider_observation_output_mismatch"

    unresolved = build_provider_observation(
        request,
        provider_name="observable-provider",
        model_version="observable-v1",
        response_id="response-unresolved",
        response_status="completed",
        output_text='{"answer": 1}',
        observation_source="in_process_provider_echo",
        acknowledged_request_binding_sha256="0" * 64,
    )
    assert verify_provider_observation(
        unresolved,
        request,
        output_text='{"answer": 1}',
    )[1] == "provider_request_acknowledgement_mismatch"

    transport_invalid = deepcopy(observation)
    transport_invalid["transport"]["request_submitted"] = False
    transport_invalid = _resign(transport_invalid, "observation_sha256")
    assert verify_provider_observation(
        transport_invalid,
        request,
        output_text='{"answer": 1}',
    )[1] == "provider_observation_transport_invalid"


def test_persisted_provider_observation_rejects_context_and_claim_tampering():
    request, observation = _observation()
    output_sha256 = observation["response"]["output_sha256"]
    assert validate_provider_observation_binding(
        observation,
        expected_output_sha256=output_sha256,
        expected_context_intervention_sha256=None,
    ) == (True, None)
    assert validate_provider_observation_binding(
        observation,
        expected_output_sha256="0" * 64,
        expected_context_intervention_sha256=None,
    )[1] == "provider_observation_output_mismatch"
    assert validate_provider_observation_binding(
        observation,
        expected_output_sha256=output_sha256,
        expected_context_intervention_sha256=SHA,
    )[1] == "provider_observation_context_mismatch"

    claim_tamper = deepcopy(observation)
    claim_tamper["claims"]["provider_transport_observed"] = False
    claim_tamper = _resign(claim_tamper, "observation_sha256")
    assert validate_provider_observation_binding(
        claim_tamper,
        expected_output_sha256=output_sha256,
        expected_context_intervention_sha256=None,
    )[1] == "provider_observation_claim_boundary_invalid"
    boundary_tamper = deepcopy(observation)
    boundary_tamper["provider"]["identity_cryptographically_verified"] = True
    boundary_tamper = _resign(boundary_tamper, "observation_sha256")
    assert verify_provider_observation(
        boundary_tamper,
        request,
        output_text='{"answer": 1}',
    )[1] == "provider_observation_boundary_invalid"
    source_tamper = deepcopy(observation)
    source_tamper["observation_source"] = "caller_claim"
    source_tamper = _resign(source_tamper, "observation_sha256")
    assert verify_provider_observation(
        source_tamper,
        request,
        output_text='{"answer": 1}',
    )[1] == "provider_observation_source_invalid"

    with pytest.raises(
        ProviderObservationError,
        match="provider_observation_not_canonicalizable",
    ):
        canonical_provider_value_sha256({"not_finite": float("nan")})


def test_live_pair_plan_and_execution_fail_closed_on_invalid_contracts():
    with pytest.raises(LivePairingError, match="task_set_invalid"):
        build_live_pair_plan([], "a" * 64, SHA)
    with pytest.raises(LivePairingError, match="task_set_invalid"):
        build_live_pair_plan(["task", "task"], "a" * 64, SHA)
    with pytest.raises(LivePairingError, match="randomization_nonce_invalid"):
        build_live_pair_plan(["task"], "short", SHA)
    with pytest.raises(LivePairingError, match="context_digest_invalid"):
        build_live_pair_plan(["task"], "a" * 64, "invalid")

    plan = build_live_pair_plan(["task-a", "task-b"], "a" * 64, SHA)
    assert validate_live_pair_plan(plan) == (True, None)
    assert validate_live_pair_plan(None)[1] == "live_pair_plan_invalid"
    assert validate_live_pair_plan({})[1] == "live_pair_plan_invalid"
    invalid_plan = deepcopy(plan)
    invalid_plan["task_ids"] = []
    assert validate_live_pair_plan(invalid_plan)[1] == "live_pair_plan_invalid"
    mismatched_plan = deepcopy(plan)
    mismatched_plan["manifest_sha256"] = "0" * 64
    assert validate_live_pair_plan(mismatched_plan)[1] == "live_pair_plan_mismatch"

    with pytest.raises(LivePairingError, match="plan_mismatch"):
        build_live_pair_execution(mismatched_plan, "task-a", "control")
    with pytest.raises(LivePairingError, match="condition_invalid"):
        build_live_pair_execution(plan, "task-a", "invalid")
    with pytest.raises(LivePairingError, match="task_not_planned"):
        build_live_pair_execution(plan, "task-missing", "control")


def test_live_pair_execution_and_set_reject_resigned_surface_attacks():
    task_ids = ["task-a", "task-b"]
    plan = build_live_pair_plan(task_ids, "a" * 64, SHA)
    control = build_live_pair_execution(plan, "task-a", "control")
    assert validate_live_pair_execution(control) == (True, None)
    assert validate_live_pair_execution(None)[1] == "live_pair_execution_missing"
    digest_tamper = deepcopy(control)
    digest_tamper["condition_position"] = 2
    assert validate_live_pair_execution(digest_tamper)[1] == (
        "live_pair_execution_digest_mismatch"
    )
    field_tamper = deepcopy(control)
    field_tamper["selected_arm"] = "maximized"
    field_tamper = _resign_live(field_tamper)
    assert validate_live_pair_execution(field_tamper)[1] == (
        "live_pair_execution_field_invalid"
    )
    balance_tamper = deepcopy(control)
    balance_tamper["condition_order_balance"] = {
        "control_first": 9,
        "treatment_first": 0,
    }
    balance_tamper = _resign_live(balance_tamper)
    assert validate_live_pair_execution(balance_tamper)[1] == (
        "live_pair_execution_balance_invalid"
    )
    assert validate_live_pair_execution(
        control,
        expected_task_id="other",
    )[1] == "live_pair_execution_task_mismatch"
    assert validate_live_pair_execution(
        control,
        expected_condition="treatment",
    )[1] == "live_pair_execution_condition_mismatch"
    assert validate_live_pair_execution(
        control,
        expected_selected_arm="maximized",
    )[1] == "live_pair_execution_arm_mismatch"
    assert validate_live_pair_execution(
        control,
        expected_context_intervention_sha256="2" * 64,
    )[1] == "live_pair_execution_context_mismatch"

    all_values = [
        build_live_pair_execution(plan, task_id, condition)
        for task_id in task_ids
        for condition in ("control", "treatment")
    ]
    assert validate_live_pair_execution_set(
        all_values,
        expected_task_ids=task_ids,
        expected_context_intervention_sha256=SHA,
    ) == (True, None)
    assert validate_live_pair_execution_set(
        [],
        expected_task_ids=task_ids,
        expected_context_intervention_sha256=SHA,
    )[1] == "live_pair_execution_coverage_incomplete"
    bad_nonce = deepcopy(all_values)
    bad_nonce[0]["randomization_nonce"] = None
    assert validate_live_pair_execution_set(
        bad_nonce,
        expected_task_ids=task_ids,
        expected_context_intervention_sha256=SHA,
    )[1] == "live_pair_randomization_nonce_invalid"
    assert validate_live_pair_execution_set(
        all_values[:2],
        expected_task_ids=["bad task"],
        expected_context_intervention_sha256=SHA,
    )[1] == "live_pair_task_set_invalid"
    assert validate_live_pair_execution_set(
        [control, control],
        expected_task_ids=["task-a"],
        expected_context_intervention_sha256=SHA,
    )[1] == "live_pair_execution_duplicate"

    other_plan = build_live_pair_plan(["task-a"], "b" * 64, SHA)
    mixed = [
        control,
        build_live_pair_execution(other_plan, "task-a", "treatment"),
    ]
    assert validate_live_pair_execution_set(
        mixed,
        expected_task_ids=["task-a"],
        expected_context_intervention_sha256=SHA,
    )[1] == "live_pair_execution_set_mismatch"
