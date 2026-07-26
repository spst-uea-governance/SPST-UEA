import pytest

from spst_runtime.evaluation.quality_evidence import (
    IndependentExactJsonScorer,
    build_producer_scoring_material,
    contract_compliance_proxy,
    normalize_quality_rubric,
    paired_hoeffding_interval,
    proxy_calibration,
    quality_rubric_digest,
    quality_claim_boundary,
    unresolved_task_quality,
    validate_contract_compliance_proxy,
    validate_producer_scoring_material,
)


def _case(baseline: object, maximized: object) -> dict[str, object]:
    return {
        "baseline": {"contract_score": baseline},
        "maximized": {"contract_score": maximized},
    }


def test_positive_contract_proxy_cannot_become_task_quality_claim():
    proxy = contract_compliance_proxy([_case(0.0, 1.0)], True)
    task_quality = unresolved_task_quality(proxy)
    calibration = proxy_calibration(proxy)
    claim = quality_claim_boundary(proxy)

    assert proxy["available"] is True
    assert proxy["paired_delta"] == 1.0
    assert proxy["claim_eligible"] is False
    assert validate_contract_compliance_proxy(proxy) == (True, None)
    assert calibration["status"] == "improved"
    assert calibration["semantic_task_quality_established"] is False
    assert task_quality["available"] is False
    assert task_quality["paired_delta"] is None
    assert claim["eligible"] is False
    assert "contract_proxy_not_semantic_task_quality" in claim["reasons"]


@pytest.mark.parametrize("score", [True, 1, -0.1, 1.1, None, "1.0"])
def test_invalid_or_non_float_proxy_scores_fail_closed(score: object):
    proxy = contract_compliance_proxy([_case(0.0, score)], True)

    assert proxy["available"] is False
    assert proxy["paired_delta"] is None
    assert unresolved_task_quality(proxy)["available"] is False


def test_empty_or_unsupported_proxy_has_no_vacuous_success():
    assert contract_compliance_proxy([], True)["available"] is False
    assert contract_compliance_proxy([_case(0.0, 1.0)], False)["available"] is False


@pytest.mark.parametrize("case", [None, {}, {"baseline": None}, {"maximized": []}])
def test_malformed_case_shape_fails_closed(case: object):
    proxy = contract_compliance_proxy([case], True)  # type: ignore[list-item]

    assert proxy["available"] is False
    assert proxy_calibration(proxy)["status"] == "scaffold_only"


def test_forged_available_proxy_with_invalid_delta_cannot_calibrate_as_improved():
    proxy = {
        "available": True,
        "paired_delta": "1.0",
        "metric_scope": "forged",
    }

    assert proxy_calibration(proxy)["status"] == "scaffold_only"
    assert quality_claim_boundary(proxy)["eligible"] is False


@pytest.mark.parametrize(
    "update",
    [
        {"schema": "forged"},
        {"metric_scope": "forged"},
        {"available": "yes"},
        {"semantic_task_quality_established": True},
        {"claim_eligible": True},
        {"pair_count": True},
        {"pair_count": 0},
        {"baseline_mean": float("nan")},
        {"maximized_mean": float("inf")},
        {"paired_delta": 0.5},
        {"paired_delta": 1},
    ],
)
def test_proxy_schema_types_and_internal_arithmetic_fail_closed(
    update: dict[str, object],
):
    proxy = contract_compliance_proxy([_case(0.0, 1.0)], True)
    proxy.update(update)

    valid, reason = validate_contract_compliance_proxy(proxy)

    assert valid is False
    assert reason is not None
    assert proxy_calibration(proxy)["status"] == "scaffold_only"
    assert unresolved_task_quality(proxy)["proxy_scores_available"] is False


def _quality_rubric() -> dict[str, object]:
    return {
        "schema": "task-specific-json-rubric-v1",
        "criteria": [
            {
                "id": "correct-answer",
                "json_pointer": "/result/answer",
                "expected": 1,
                "weight": 1.0,
            }
        ],
    }


def test_task_specific_rubric_projects_and_scores_only_anonymous_json_values():
    rubric, reason = normalize_quality_rubric(_quality_rubric())
    assert reason is None
    assert rubric is not None
    material = build_producer_scoring_material(
        '{"metadata":"ignored","result":{"answer":1.0}}',
        rubric,
    )

    valid, material_reason = validate_producer_scoring_material(material)
    scores = IndependentExactJsonScorer().score(
        rubric,
        [{"slot_id": "slot-anonymous", "material": material}],
    )

    assert valid is True
    assert material_reason is None
    assert material["status"] == "ready"
    assert material["rubric_digest"] == quality_rubric_digest(rubric)
    assert "metadata" not in str(material)
    assert scores[0]["score"] == 1.0
    assert set(scores[0]) == {"slot_id", "score", "criteria", "material_digest"}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"result":{"answer":1},"result":{"answer":0}}', "artifact_format_unresolved"),
        ("not-json", "artifact_format_unresolved"),
        ('{"result":{"answer":NaN}}', "artifact_format_unresolved"),
    ],
)
def test_invalid_scoring_artifact_fails_closed(text: str, reason: str):
    rubric, _ = normalize_quality_rubric(_quality_rubric())
    material = build_producer_scoring_material(text, rubric)

    assert material["status"] == "unresolved"
    assert material["reason"] == reason
    assert validate_producer_scoring_material(material) == (True, None)
    with pytest.raises(ValueError, match="scoring_material_unresolved"):
        IndependentExactJsonScorer().score(
            rubric,  # type: ignore[arg-type]
            [{"slot_id": "slot-anonymous", "material": material}],
        )


@pytest.mark.parametrize(
    "update",
    [
        {"schema": "forged"},
        {"criteria": []},
        {
            "criteria": [
                {
                    "id": "correct-answer",
                    "json_pointer": "/answer",
                    "expected": 1,
                    "weight": 0.9,
                }
            ]
        },
        {
            "criteria": [
                {
                    "id": "correct-answer",
                    "json_pointer": "answer",
                    "expected": 1,
                    "weight": 1.0,
                }
            ]
        },
    ],
)
def test_invalid_quality_rubric_is_rejected_as_a_whole(update: dict[str, object]):
    value = {**_quality_rubric(), **update}

    rubric, reason = normalize_quality_rubric(value)

    assert rubric is None
    assert reason is not None


def test_hoeffding_bound_is_conservative_and_requires_float_bounded_pairs():
    interval = paired_hoeffding_interval([1.0] * 8)

    assert interval == {
        "available": True,
        "method": "hoeffding_bounded_paired_delta",
        "confidence_level": 0.95,
        "lower_bound": 0.039677,
        "upper_bound": 1.0,
        "half_width": 0.960323,
    }
    assert paired_hoeffding_interval([])["available"] is False
    assert paired_hoeffding_interval([1]) ["available"] is False  # type: ignore[list-item]
