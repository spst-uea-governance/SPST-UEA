import pytest

from spst_runtime.evaluation.operational_shadow import OperationalShadowRunner
from spst_runtime.model_artifact_contract import (
    ModelArtifactContractError,
    build_model_artifact_contract,
    provider_artifact_schema,
    serialize_provider_artifact,
    validate_model_artifact_contract,
)


def test_structured_artifact_contract_is_typed_closed_and_canonical():
    contract = build_model_artifact_contract(
        ["answer", "score"],
        {"answer": "string", "score": "integer"},
    )

    assert validate_model_artifact_contract(contract) == (True, None)
    assert provider_artifact_schema(contract) == {
        "type": "object",
        "required": ["answer", "score"],
        "properties": {
            "answer": {"type": "string"},
            "score": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    assert serialize_provider_artifact(
        {"score": 1, "answer": "ok"}, contract
    ) == '{"answer":"ok","score":1}'


@pytest.mark.parametrize(
    "keys,types,reason",
    [
        (["answer", "answer"], {"answer": "string"}, "required_keys_invalid"),
        (["bad key"], {"bad key": "string"}, "required_keys_invalid"),
        (["answer"], {}, "property_types_invalid"),
        (["answer"], {"answer": "object"}, "property_types_invalid"),
    ],
)
def test_structured_artifact_contract_rejects_ambiguous_or_unsafe_shapes(
    keys, types, reason
):
    with pytest.raises(ModelArtifactContractError, match=reason):
        build_model_artifact_contract(keys, types)


@pytest.mark.parametrize(
    "artifact",
    [
        "unknown",
        {"answer": "ok", "extra": "surface inflation"},
        {"answer": 1},
        {},
    ],
)
def test_structured_artifact_contract_rejects_wrong_root_extra_fields_and_types(
    artifact,
):
    contract = build_model_artifact_contract(
        ["answer"], {"answer": "string"}
    )

    with pytest.raises(
        ModelArtifactContractError,
        match="provider_artifact_json_contract_mismatch",
    ):
        serialize_provider_artifact(artifact, contract)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_structured_artifact_contract_rejects_non_json_numbers(value):
    contract = build_model_artifact_contract(
        ["score"], {"score": "number"}
    )

    with pytest.raises(
        ModelArtifactContractError,
        match="provider_artifact_json_contract_mismatch",
    ):
        serialize_provider_artifact({"score": value}, contract)


def test_text_artifact_contract_preserves_text_and_rejects_objects():
    contract = build_model_artifact_contract([])

    assert serialize_provider_artifact("unknown", contract) == "unknown"
    with pytest.raises(
        ModelArtifactContractError,
        match="provider_artifact_text_invalid",
    ):
        serialize_provider_artifact({"answer": "unknown"}, contract)


def test_artifact_contract_validation_rejects_field_and_kind_relabelling():
    contract = build_model_artifact_contract(
        ["answer"], {"answer": "string"}
    )

    assert validate_model_artifact_contract({**contract, "extra": True}) == (
        False,
        "artifact_contract_fields_invalid",
    )
    assert validate_model_artifact_contract({**contract, "kind": "text"}) == (
        False,
        "artifact_contract_kind_mismatch",
    )


def test_operational_shadow_derives_flat_primitive_types_from_registered_rubric():
    task = {
        "expected_json_keys": ["text", "count", "ratio", "flag", "empty"],
        "quality_rubric": {
            "criteria": [
                {"json_pointer": "/text", "expected": "ok"},
                {"json_pointer": "/count", "expected": 1},
                {"json_pointer": "/ratio", "expected": 0.5},
                {"json_pointer": "/flag", "expected": True},
                {"json_pointer": "/empty", "expected": None},
            ]
        },
    }

    assert OperationalShadowRunner._model_artifact_contract(task) == (
        build_model_artifact_contract(
            ["text", "count", "ratio", "flag", "empty"],
            {
                "text": "string",
                "count": "integer",
                "ratio": "number",
                "flag": "boolean",
                "empty": "null",
            },
        )
    )


@pytest.mark.parametrize(
    "criteria",
    [
        [],
        [{"json_pointer": "/answer", "expected": "a"}] * 2,
        [{"json_pointer": "/answer/nested", "expected": "a"}],
        [{"json_pointer": "/answer", "expected": {"nested": "a"}}],
    ],
)
def test_operational_shadow_does_not_invent_unsupported_artifact_schema(criteria):
    assert OperationalShadowRunner._model_artifact_contract(
        {
            "expected_json_keys": ["answer"],
            "quality_rubric": {"criteria": criteria},
        }
    ) is None
