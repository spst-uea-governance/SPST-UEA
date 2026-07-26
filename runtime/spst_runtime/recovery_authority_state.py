import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


RECOVERY_AUTHORITY_STATE_SCHEMA = "spst-recovery-authority-state-v1"
RECOVERY_AUTHORITY_ROLLBACK_ANCHOR_SCHEMA = (
    "spst-recovery-authority-rollback-anchor-v1"
)
RECOVERY_KEY_CUSTODY_EVIDENCE_SCHEMA = "spst-recovery-key-custody-evidence-v1"
RECOVERY_KEY_CUSTODY_POLICY_SCHEMA = "spst-recovery-key-custody-policy-v1"
RECOVERY_AUTHORITY_STATE_ALGORITHM = "Ed25519"

_sha256 = re.compile(r"^[0-9a-f]{64}$")
_CUSTODY_PROVIDERS = frozenset({"local_file", "windows_dpapi", "pkcs11", "tpm"})


class RecoveryAuthorityStateError(ValueError):
    """Reject invalid revocation, time-floor, custody, or rollback evidence."""


def default_recovery_key_custody_policy() -> dict[str, Any]:
    return {
        "schema": RECOVERY_KEY_CUSTODY_POLICY_SCHEMA,
        "allowed_providers": ["local_file"],
        "require_os_protection": False,
        "require_hardware_backing": False,
        "require_external_attestation": False,
    }


def build_recovery_key_custody_evidence(
    key_id: str,
    *,
    provider: str = "local_file",
    os_protected: bool = False,
    hardware_backed: bool = False,
    external_attestation_verified: bool = False,
) -> dict[str, Any]:
    unsigned = {
        "schema": RECOVERY_KEY_CUSTODY_EVIDENCE_SCHEMA,
        "key_id": key_id,
        "provider": provider,
        "os_protected": os_protected,
        "hardware_backed": hardware_backed,
        "external_attestation_verified": external_attestation_verified,
    }
    evidence = {**unsigned, "evidence_sha256": _canonical_hash(unsigned)}
    reason = _key_custody_evidence_reason(evidence)
    if reason:
        raise RecoveryAuthorityStateError(reason)
    return evidence


def validate_recovery_key_custody_policy(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "recovery_key_custody_policy_missing"
    providers = value.get("allowed_providers")
    if (
        value.get("schema") != RECOVERY_KEY_CUSTODY_POLICY_SCHEMA
        or not isinstance(providers, list)
        or not providers
        or providers != sorted(set(providers))
        or any(provider not in _CUSTODY_PROVIDERS for provider in providers)
        or not all(
            isinstance(value.get(field), bool)
            for field in (
                "require_os_protection",
                "require_hardware_backing",
                "require_external_attestation",
            )
        )
    ):
        return "recovery_key_custody_policy_invalid"
    return None


def issue_recovery_authority_state(
    trust_anchor: dict[str, Any],
    root_private_key_file: str | Path,
    key_custody_evidence: dict[str, Any],
    *,
    generation: int,
    previous_state_sha256: str | None = None,
    revoked_operator_certificate_sha256s: list[str] | None = None,
    revoked_grant_sha256s: list[str] | None = None,
    issued_at_ms: int | None = None,
    valid_until_ms: int | None = None,
    minimum_accepted_time_ms: int | None = None,
) -> dict[str, Any]:
    trust_reason = _trust_anchor_reason(trust_anchor)
    if trust_reason:
        raise RecoveryAuthorityStateError(trust_reason)
    root_key = _load_private_key(root_private_key_file)
    if _public_key_id(root_key.public_key()) != trust_anchor.get("root_key_id"):
        raise RecoveryAuthorityStateError("recovery_authority_root_key_mismatch")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise RecoveryAuthorityStateError("recovery_authority_state_generation_invalid")
    if (generation == 1 and previous_state_sha256 is not None) or (
        generation > 1 and not _is_sha256(previous_state_sha256)
    ):
        raise RecoveryAuthorityStateError("recovery_authority_state_chain_invalid")
    issued = _now_ms() if issued_at_ms is None else issued_at_ms
    valid_until = issued + 86_400_000 if valid_until_ms is None else valid_until_ms
    time_floor = issued if minimum_accepted_time_ms is None else minimum_accepted_time_ms
    if (
        not _valid_time_window(issued, valid_until)
        or not isinstance(time_floor, int)
        or isinstance(time_floor, bool)
        or not issued <= time_floor <= valid_until
    ):
        raise RecoveryAuthorityStateError("recovery_authority_state_time_window_invalid")
    custody_reason = _key_custody_evidence_reason(key_custody_evidence)
    if custody_reason:
        raise RecoveryAuthorityStateError(custody_reason)
    if key_custody_evidence.get("key_id") != trust_anchor.get("root_key_id"):
        raise RecoveryAuthorityStateError("recovery_key_custody_key_mismatch")
    revoked_certificates = _normalized_digests(
        revoked_operator_certificate_sha256s or [],
        reason="recovery_authority_certificate_revocations_invalid",
    )
    revoked_grants = _normalized_digests(
        revoked_grant_sha256s or [],
        reason="recovery_authority_grant_revocations_invalid",
    )
    unsigned = {
        "schema": RECOVERY_AUTHORITY_STATE_SCHEMA,
        "algorithm": RECOVERY_AUTHORITY_STATE_ALGORITHM,
        "root_key_id": trust_anchor["root_key_id"],
        "generation": generation,
        "previous_state_sha256": previous_state_sha256,
        "issued_at_ms": issued,
        "valid_until_ms": valid_until,
        "minimum_accepted_time_ms": time_floor,
        "revoked_operator_certificate_sha256s": revoked_certificates,
        "revoked_grant_sha256s": revoked_grants,
        "key_custody_evidence": deepcopy(key_custody_evidence),
        "trusted_time_source_verified": False,
        "hardware_rollback_counter_verified": False,
    }
    state_sha256 = _canonical_hash(unsigned)
    signed = {**unsigned, "state_sha256": state_sha256}
    return {
        **signed,
        "root_signature": _encode(root_key.sign(_canonical_bytes(signed))),
    }


def issue_recovery_rollback_anchor(
    trust_anchor: dict[str, Any],
    root_private_key_file: str | Path,
    authority_state: dict[str, Any],
    *,
    previous_anchor_sha256: str | None = None,
) -> dict[str, Any]:
    state_projection, state_reason = verify_recovery_authority_state(
        authority_state,
        trust_anchor,
        rollback_anchor=None,
        verification_time_ms=int(authority_state.get("issued_at_ms") or 0),
        require_rollback_anchor=False,
    )
    if state_reason or state_projection is None:
        raise RecoveryAuthorityStateError(
            state_reason or "recovery_authority_state_invalid"
        )
    generation = int(authority_state["generation"])
    if (generation == 1 and previous_anchor_sha256 is not None) or (
        generation > 1 and not _is_sha256(previous_anchor_sha256)
    ):
        raise RecoveryAuthorityStateError("recovery_authority_anchor_chain_invalid")
    root_key = _load_private_key(root_private_key_file)
    if _public_key_id(root_key.public_key()) != trust_anchor.get("root_key_id"):
        raise RecoveryAuthorityStateError("recovery_authority_root_key_mismatch")
    unsigned = {
        "schema": RECOVERY_AUTHORITY_ROLLBACK_ANCHOR_SCHEMA,
        "algorithm": RECOVERY_AUTHORITY_STATE_ALGORITHM,
        "root_key_id": trust_anchor["root_key_id"],
        "generation": generation,
        "authority_state_sha256": authority_state["state_sha256"],
        "previous_anchor_sha256": previous_anchor_sha256,
        "local_dual_copy_only": True,
        "external_monotonic_counter_verified": False,
    }
    anchor_sha256 = _canonical_hash(unsigned)
    signed = {**unsigned, "anchor_sha256": anchor_sha256}
    return {
        **signed,
        "root_signature": _encode(root_key.sign(_canonical_bytes(signed))),
    }


def verify_recovery_authority_state(
    value: Any,
    trust_anchor: dict[str, Any],
    *,
    rollback_anchor: dict[str, Any] | None,
    verification_time_ms: int | None = None,
    minimum_generation: int | None = None,
    expected_state_sha256: str | None = None,
    expected_anchor_sha256: str | None = None,
    revoked_operator_certificate_sha256: str | None = None,
    revoked_grant_sha256: str | None = None,
    require_rollback_anchor: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    trust_reason = _trust_anchor_reason(trust_anchor)
    if trust_reason:
        return None, trust_reason
    state_reason = _authority_state_reason(value, trust_anchor)
    if state_reason:
        return None, state_reason
    state = value
    current = _now_ms() if verification_time_ms is None else verification_time_ms
    if current < int(state["minimum_accepted_time_ms"]):
        return None, "recovery_authority_clock_rollback_detected"
    if current > int(state["valid_until_ms"]):
        return None, "recovery_authority_state_expired"
    required_generation = (
        int(trust_anchor.get("minimum_authority_state_generation") or 0)
        if minimum_generation is None
        else minimum_generation
    )
    if (
        not isinstance(required_generation, int)
        or isinstance(required_generation, bool)
        or int(state["generation"]) < required_generation
    ):
        return None, "recovery_authority_state_generation_rollback"
    if expected_state_sha256 is not None and state.get(
        "state_sha256"
    ) != expected_state_sha256:
        return None, "recovery_authority_state_binding_mismatch"
    policy = trust_anchor.get("key_custody_policy")
    policy = policy if isinstance(policy, dict) else default_recovery_key_custody_policy()
    policy_reason = validate_recovery_key_custody_policy(policy)
    if policy_reason:
        return None, policy_reason
    custody = state["key_custody_evidence"]
    custody_reason = _custody_policy_reason(custody, policy)
    if custody_reason:
        return None, custody_reason
    anchor_projection: dict[str, Any] | None = None
    if rollback_anchor is not None:
        anchor_reason = _rollback_anchor_reason(rollback_anchor, state, trust_anchor)
        if anchor_reason:
            return None, anchor_reason
        if expected_anchor_sha256 is not None and rollback_anchor.get(
            "anchor_sha256"
        ) != expected_anchor_sha256:
            return None, "recovery_authority_anchor_binding_mismatch"
        anchor_projection = {
            "anchor_sha256": rollback_anchor["anchor_sha256"],
            "generation": rollback_anchor["generation"],
            "external_monotonic_counter_verified": False,
            "rollback_scope": "root_signed_local_dual_copy",
        }
    elif require_rollback_anchor:
        return None, "recovery_authority_rollback_anchor_required"
    if revoked_operator_certificate_sha256 in state[
        "revoked_operator_certificate_sha256s"
    ]:
        return None, "recovery_operator_certificate_revoked"
    if revoked_grant_sha256 in state["revoked_grant_sha256s"]:
        return None, "recovery_authority_grant_revoked"
    return (
        {
            "schema": RECOVERY_AUTHORITY_STATE_SCHEMA,
            "state_sha256": state["state_sha256"],
            "generation": state["generation"],
            "minimum_accepted_time_ms": state["minimum_accepted_time_ms"],
            "authority_state_verified": True,
            "revocation_checked": True,
            "clock_rollback_checked": True,
            "trusted_time_source_verified": False,
            "key_custody_evidence_sha256": custody["evidence_sha256"],
            "key_custody_provider": custody["provider"],
            "os_key_protection_verified": (
                custody["os_protected"]
                and custody["external_attestation_verified"]
            ),
            "hardware_key_custody_verified": (
                custody["hardware_backed"]
                and custody["external_attestation_verified"]
            ),
            "rollback_anchor": anchor_projection,
            "full_rollback_resistance_verified": False,
            "verified": True,
        },
        None,
    )


def _authority_state_reason(value: Any, trust_anchor: dict[str, Any]) -> str | None:
    if not isinstance(value, dict):
        return "recovery_authority_state_required"
    unsigned = {
        key: item
        for key, item in value.items()
        if key not in {"state_sha256", "root_signature"}
    }
    signed = {**unsigned, "state_sha256": value.get("state_sha256")}
    try:
        root_public = Ed25519PublicKey.from_public_bytes(
            _decode_public_key(trust_anchor.get("root_public_key"))
        )
        root_public.verify(
            _decode(value.get("root_signature")),
            _canonical_bytes(signed),
        )
    except (RecoveryAuthorityStateError, InvalidSignature, ValueError):
        return "recovery_authority_state_signature_invalid"
    generation = value.get("generation")
    previous = value.get("previous_state_sha256")
    custody_reason = _key_custody_evidence_reason(value.get("key_custody_evidence"))
    certificate_revocations = _valid_digest_list(
        value.get("revoked_operator_certificate_sha256s")
    )
    grant_revocations = _valid_digest_list(value.get("revoked_grant_sha256s"))
    if (
        value.get("schema") != RECOVERY_AUTHORITY_STATE_SCHEMA
        or value.get("algorithm") != RECOVERY_AUTHORITY_STATE_ALGORITHM
        or value.get("root_key_id") != trust_anchor.get("root_key_id")
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or (generation == 1 and previous is not None)
        or (generation > 1 and not _is_sha256(previous))
        or not _valid_time_window(
            value.get("issued_at_ms"), value.get("valid_until_ms")
        )
        or not isinstance(value.get("minimum_accepted_time_ms"), int)
        or isinstance(value.get("minimum_accepted_time_ms"), bool)
        or not int(value["issued_at_ms"])
        <= int(value["minimum_accepted_time_ms"])
        <= int(value["valid_until_ms"])
        or certificate_revocations is None
        or grant_revocations is None
        or custody_reason is not None
        or value.get("key_custody_evidence", {}).get("key_id")
        != trust_anchor.get("root_key_id")
        or value.get("trusted_time_source_verified") is not False
        or value.get("hardware_rollback_counter_verified") is not False
        or not _is_sha256(value.get("state_sha256"))
        or value.get("state_sha256") != _canonical_hash(unsigned)
    ):
        return custody_reason or "recovery_authority_state_invalid"
    return None


def _rollback_anchor_reason(
    value: Any,
    state: dict[str, Any],
    trust_anchor: dict[str, Any],
) -> str | None:
    if not isinstance(value, dict):
        return "recovery_authority_rollback_anchor_required"
    unsigned = {
        key: item
        for key, item in value.items()
        if key not in {"anchor_sha256", "root_signature"}
    }
    signed = {**unsigned, "anchor_sha256": value.get("anchor_sha256")}
    try:
        root_public = Ed25519PublicKey.from_public_bytes(
            _decode_public_key(trust_anchor.get("root_public_key"))
        )
        root_public.verify(
            _decode(value.get("root_signature")),
            _canonical_bytes(signed),
        )
    except (RecoveryAuthorityStateError, InvalidSignature, ValueError):
        return "recovery_authority_rollback_anchor_signature_invalid"
    generation = value.get("generation")
    previous = value.get("previous_anchor_sha256")
    if (
        value.get("schema") != RECOVERY_AUTHORITY_ROLLBACK_ANCHOR_SCHEMA
        or value.get("algorithm") != RECOVERY_AUTHORITY_STATE_ALGORITHM
        or value.get("root_key_id") != trust_anchor.get("root_key_id")
        or generation != state.get("generation")
        or value.get("authority_state_sha256") != state.get("state_sha256")
        or (generation == 1 and previous is not None)
        or (isinstance(generation, int) and generation > 1 and not _is_sha256(previous))
        or value.get("local_dual_copy_only") is not True
        or value.get("external_monotonic_counter_verified") is not False
        or not _is_sha256(value.get("anchor_sha256"))
        or value.get("anchor_sha256") != _canonical_hash(unsigned)
    ):
        return "recovery_authority_rollback_anchor_invalid"
    return None


def _key_custody_evidence_reason(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "recovery_key_custody_evidence_missing"
    unsigned = {key: item for key, item in value.items() if key != "evidence_sha256"}
    provider = value.get("provider")
    if (
        value.get("schema") != RECOVERY_KEY_CUSTODY_EVIDENCE_SCHEMA
        or not _is_sha256(value.get("key_id"))
        or provider not in _CUSTODY_PROVIDERS
        or not all(
            isinstance(value.get(field), bool)
            for field in (
                "os_protected",
                "hardware_backed",
                "external_attestation_verified",
            )
        )
        or value.get("external_attestation_verified") is not False
        or (
            provider == "local_file"
            and any(
                value.get(field) is not False
                for field in (
                    "os_protected",
                    "hardware_backed",
                )
            )
        )
        or not _is_sha256(value.get("evidence_sha256"))
        or value.get("evidence_sha256") != _canonical_hash(unsigned)
    ):
        return "recovery_key_custody_evidence_invalid"
    return None


def _custody_policy_reason(
    evidence: dict[str, Any],
    policy: dict[str, Any],
) -> str | None:
    if evidence.get("provider") not in policy["allowed_providers"]:
        return "recovery_key_custody_provider_rejected"
    if policy["require_os_protection"] and not (
        evidence.get("os_protected") is True
        and evidence.get("external_attestation_verified") is True
    ):
        return "recovery_key_os_protection_unverified"
    if policy["require_hardware_backing"] and not (
        evidence.get("hardware_backed") is True
        and evidence.get("external_attestation_verified") is True
    ):
        return "recovery_key_hardware_custody_unverified"
    if policy["require_external_attestation"] and evidence.get(
        "external_attestation_verified"
    ) is not True:
        return "recovery_key_custody_attestation_unverified"
    return None


def _trust_anchor_reason(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "recovery_authority_trust_anchor_missing"
    unsigned = {key: item for key, item in value.items() if key != "trust_anchor_sha256"}
    try:
        public = _decode_public_key(value.get("root_public_key"))
    except RecoveryAuthorityStateError as error:
        return str(error)
    if (
        value.get("schema") != "spst-recovery-authority-trust-v1"
        or value.get("algorithm") != RECOVERY_AUTHORITY_STATE_ALGORITHM
        or value.get("root_key_id") != hashlib.sha256(public).hexdigest()
        or not _is_sha256(value.get("trust_anchor_sha256"))
        or value.get("trust_anchor_sha256") != _canonical_hash(unsigned)
    ):
        return "recovery_authority_trust_anchor_invalid"
    return None


def _load_private_key(path: str | Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(
            Path(path).expanduser().resolve().read_bytes(),
            password=None,
        )
    except (OSError, ValueError, TypeError) as error:
        raise RecoveryAuthorityStateError("recovery_private_key_invalid") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise RecoveryAuthorityStateError("recovery_private_key_type_invalid")
    return key


def _public_key_id(value: Ed25519PublicKey) -> str:
    return hashlib.sha256(
        value.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).hexdigest()


def _decode_public_key(value: Any) -> bytes:
    decoded = _decode(value)
    if len(decoded) != 32:
        raise RecoveryAuthorityStateError("recovery_public_key_invalid")
    return decoded


def _decode(value: Any) -> bytes:
    if not isinstance(value, str):
        raise RecoveryAuthorityStateError("recovery_signature_encoding_invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise RecoveryAuthorityStateError("recovery_signature_encoding_invalid") from error


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _sha256.fullmatch(value) is not None


def _valid_time_window(start: Any, end: Any) -> bool:
    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
    )


def _valid_digest_list(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or value != sorted(set(value))
        or any(not _is_sha256(item) for item in value)
    ):
        return None
    return value


def _normalized_digests(values: list[str], *, reason: str) -> list[str]:
    normalized = sorted(set(values))
    if len(normalized) != len(values) or any(not _is_sha256(item) for item in values):
        raise RecoveryAuthorityStateError(reason)
    return normalized


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
