from copy import deepcopy
import hashlib
import json
import re
from typing import Any


LIVE_PAIR_PLAN_SCHEMA = "spst-live-pair-execution-plan-v1"
LIVE_PAIR_EXECUTION_SCHEMA = "spst-live-pair-execution-v1"
LIVE_PAIR_RANDOMIZATION_METHOD = "sha256_balanced_condition_order_v1"
CONDITIONS = ("control", "treatment")

_identifier = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class LivePairingError(ValueError):
    """Raised when a live paired execution schedule is incomplete or altered."""


def build_live_pair_plan(
    task_ids: list[str],
    nonce: str,
    context_intervention_sha256: str,
) -> dict[str, Any]:
    normalized = sorted(task_ids)
    if (
        not normalized
        or len(normalized) != len(set(normalized))
        or any(not _safe_identifier(task_id) for task_id in normalized)
    ):
        raise LivePairingError("live_pair_task_set_invalid")
    if not isinstance(nonce, str) or not 32 <= len(nonce) <= 256:
        raise LivePairingError("live_pair_randomization_nonce_invalid")
    if not _is_sha256(context_intervention_sha256):
        raise LivePairingError("live_pair_context_digest_invalid")

    ranked = sorted(
        normalized,
        key=lambda task_id: _sha256_text(f"{nonce}:{task_id}"),
    )
    control_first = set(ranked[: (len(ranked) + 1) // 2])
    orders = [
        {
            "task_id": task_id,
            "condition_order": (
                ["control", "treatment"]
                if task_id in control_first
                else ["treatment", "control"]
            ),
        }
        for task_id in normalized
    ]
    unsigned = {
        "schema": LIVE_PAIR_PLAN_SCHEMA,
        "task_ids": normalized,
        "task_set_sha256": _canonical_hash(normalized),
        "context_intervention_sha256": context_intervention_sha256,
        "randomization_method": LIVE_PAIR_RANDOMIZATION_METHOD,
        "randomization_nonce": nonce,
        "randomization_nonce_sha256": _sha256_text(nonce),
        "condition_order_balance": {
            "control_first": len(control_first),
            "treatment_first": len(normalized) - len(control_first),
        },
        "orders": orders,
    }
    manifest_sha256 = _canonical_hash(unsigned)
    return {
        **unsigned,
        "experiment_id": f"LPAIR-{manifest_sha256[:16]}",
        "manifest_sha256": manifest_sha256,
    }


def build_live_pair_execution(
    plan: dict[str, Any],
    task_id: str,
    condition: str,
) -> dict[str, Any]:
    valid, reason = validate_live_pair_plan(plan)
    if not valid:
        raise LivePairingError(reason or "live_pair_plan_invalid")
    if condition not in CONDITIONS:
        raise LivePairingError("live_pair_condition_invalid")
    order_entry = next(
        (
            item
            for item in plan["orders"]
            if isinstance(item, dict) and item.get("task_id") == task_id
        ),
        None,
    )
    if not isinstance(order_entry, dict):
        raise LivePairingError("live_pair_task_not_planned")
    order = list(order_entry["condition_order"])
    unsigned = {
        "schema": LIVE_PAIR_EXECUTION_SCHEMA,
        "experiment_id": plan["experiment_id"],
        "manifest_sha256": plan["manifest_sha256"],
        "task_set_sha256": plan["task_set_sha256"],
        "task_id": task_id,
        "condition": condition,
        "selected_arm": "baseline" if condition == "control" else "maximized",
        "condition_order": order,
        "condition_position": order.index(condition) + 1,
        "context_intervention_sha256": plan["context_intervention_sha256"],
        "randomization_method": plan["randomization_method"],
        "randomization_nonce": plan["randomization_nonce"],
        "randomization_nonce_sha256": plan["randomization_nonce_sha256"],
        "condition_order_balance": deepcopy(plan["condition_order_balance"]),
    }
    return {**unsigned, "execution_binding_sha256": _canonical_hash(unsigned)}


def validate_live_pair_plan(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "live_pair_plan_invalid"
    task_ids = value.get("task_ids")
    nonce = value.get("randomization_nonce")
    context_sha256 = value.get("context_intervention_sha256")
    if (
        not isinstance(task_ids, list)
        or not isinstance(nonce, str)
        or not isinstance(context_sha256, str)
    ):
        return False, "live_pair_plan_invalid"
    try:
        expected = build_live_pair_plan(
            task_ids,
            nonce,
            context_sha256,
        )
    except (LivePairingError, TypeError):
        return False, "live_pair_plan_invalid"
    return (True, None) if value == expected else (False, "live_pair_plan_mismatch")


def validate_live_pair_execution(
    value: Any,
    *,
    expected_task_id: str | None = None,
    expected_condition: str | None = None,
    expected_selected_arm: str | None = None,
    expected_context_intervention_sha256: str | None = None,
) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "live_pair_execution_missing"
    digest = value.get("execution_binding_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "execution_binding_sha256"
    }
    if (
        value.get("schema") != LIVE_PAIR_EXECUTION_SCHEMA
        or not _is_sha256(digest)
        or _canonical_hash(unsigned) != digest
    ):
        return False, "live_pair_execution_digest_mismatch"
    task_id = value.get("task_id")
    condition = value.get("condition")
    selected_arm = value.get("selected_arm")
    order = value.get("condition_order")
    position = value.get("condition_position")
    if (
        not _safe_identifier(task_id)
        or condition not in CONDITIONS
        or selected_arm
        != ("baseline" if condition == "control" else "maximized")
        or not isinstance(order, list)
        or sorted(order) != list(CONDITIONS)
        or not isinstance(position, int)
        or isinstance(position, bool)
        or position not in {1, 2}
        or order[position - 1] != condition
        or not _is_sha256(value.get("manifest_sha256"))
        or not _is_sha256(value.get("task_set_sha256"))
        or not _is_sha256(value.get("context_intervention_sha256"))
        or value.get("randomization_method") != LIVE_PAIR_RANDOMIZATION_METHOD
        or not isinstance(value.get("randomization_nonce"), str)
        or not 32 <= len(value["randomization_nonce"]) <= 256
        or value.get("randomization_nonce_sha256")
        != _sha256_text(value["randomization_nonce"])
        or not _safe_identifier(value.get("experiment_id"))
    ):
        return False, "live_pair_execution_field_invalid"
    balance = value.get("condition_order_balance")
    if (
        not isinstance(balance, dict)
        or set(balance) != {"control_first", "treatment_first"}
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in balance.values()
        )
        or abs(balance["control_first"] - balance["treatment_first"]) > 1
    ):
        return False, "live_pair_execution_balance_invalid"
    if expected_task_id is not None and task_id != expected_task_id:
        return False, "live_pair_execution_task_mismatch"
    if expected_condition is not None and condition != expected_condition:
        return False, "live_pair_execution_condition_mismatch"
    if expected_selected_arm is not None and selected_arm != expected_selected_arm:
        return False, "live_pair_execution_arm_mismatch"
    if (
        expected_context_intervention_sha256 is not None
        and value.get("context_intervention_sha256")
        != expected_context_intervention_sha256
    ):
        return False, "live_pair_execution_context_mismatch"
    return True, None


def validate_live_pair_execution_set(
    values: list[dict[str, Any]],
    *,
    expected_task_ids: list[str],
    expected_context_intervention_sha256: str,
) -> tuple[bool, str | None]:
    if len(values) != len(expected_task_ids) * 2 or not values:
        return False, "live_pair_execution_coverage_incomplete"
    first = values[0]
    nonce = first.get("randomization_nonce")
    if not isinstance(nonce, str):
        return False, "live_pair_randomization_nonce_invalid"
    try:
        plan = build_live_pair_plan(
            expected_task_ids,
            nonce,
            expected_context_intervention_sha256,
        )
    except (LivePairingError, TypeError) as error:
        return False, str(error)
    expected = {
        (task_id, condition): build_live_pair_execution(plan, task_id, condition)
        for task_id in expected_task_ids
        for condition in CONDITIONS
    }
    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for value in values:
        valid, reason = validate_live_pair_execution(value)
        if not valid:
            return False, reason
        key = (str(value.get("task_id")), str(value.get("condition")))
        if key in actual:
            return False, "live_pair_execution_duplicate"
        actual[key] = value
    if actual != expected:
        return False, "live_pair_execution_set_mismatch"
    return True, None


def canonical_live_pair_hash(value: Any) -> str:
    return _canonical_hash(value)


def _canonical_hash(value: Any) -> str:
    return _sha256_text(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _identifier.fullmatch(value) else None
