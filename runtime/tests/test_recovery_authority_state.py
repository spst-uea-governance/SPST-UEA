from copy import deepcopy
import hashlib
from pathlib import Path
import time

import pytest

from spst_runtime.recovery_authority import (
    generate_recovery_authority_pki,
    generate_supervisor_attestation_key,
    issue_recovery_authority_grant,
    verify_recovery_authority_grant,
)
from spst_runtime.recovery_authority_state import (
    RECOVERY_KEY_CUSTODY_POLICY_SCHEMA,
    RecoveryAuthorityStateError,
    build_recovery_key_custody_evidence,
    issue_recovery_authority_state,
    issue_recovery_rollback_anchor,
    verify_recovery_authority_state,
)
from spst_runtime.process_transport import (
    DurableProcessProviderStore,
    ProcessTransportError,
    build_recovery_lease_resource_id,
)


NOW_MS = 1_800_000_000_000


def _materials(
    tmp_path: Path,
    *,
    custody_policy: dict | None = None,
    now_ms: int = NOW_MS,
):
    root_key = tmp_path / "root.pem"
    operator_key = tmp_path / "operator.pem"
    trust, certificate = generate_recovery_authority_pki(
        root_key,
        operator_key,
        operator_id="operator-arch12",
        valid_from_ms=now_ms - 1_000,
        valid_until_ms=now_ms + 60_000,
        authority_state_required=True,
        minimum_authority_state_generation=1,
        key_custody_policy=custody_policy,
    )
    custody = build_recovery_key_custody_evidence(trust["root_key_id"])
    state = issue_recovery_authority_state(
        trust,
        root_key,
        custody,
        generation=1,
        issued_at_ms=now_ms,
        valid_until_ms=now_ms + 30_000,
        minimum_accepted_time_ms=now_ms,
    )
    anchor = issue_recovery_rollback_anchor(trust, root_key, state)
    supervisor_key = tmp_path / "supervisor.pem"
    supervisor_public = generate_supervisor_attestation_key(supervisor_key)
    grant = issue_recovery_authority_grant(
        trust,
        certificate,
        operator_key,
        supervisor_public,
        program_id="program-arch12",
        program_sha256="1" * 64,
        attempt_id="attempt-arch12",
        provider_instance_sha256="2" * 64,
        lease_resource_id="3" * 64,
        lease_owner_id="owner-arch12",
        transport_recovery_authority_sha256="4" * 64,
        context_intervention_sha256="5" * 64,
        issued_at_ms=now_ms + 1,
        expires_at_ms=now_ms + 20_000,
        nonce="grant-arch12",
        authority_state=state,
        rollback_anchor=anchor,
    )
    return {
        "root_key": root_key,
        "operator_key": operator_key,
        "trust": trust,
        "certificate": certificate,
        "custody": custody,
        "state": state,
        "anchor": anchor,
        "supervisor_public": supervisor_public,
        "grant": grant,
    }


def test_state_bound_grant_requires_current_revocation_and_anchor_evidence(
    tmp_path: Path,
):
    material = _materials(tmp_path)

    projection, reason = verify_recovery_authority_grant(
        material["grant"],
        material["trust"],
        verification_time_ms=NOW_MS + 2,
        authority_state=material["state"],
        rollback_anchor=material["anchor"],
    )

    assert reason is None
    assert projection is not None
    assert projection["verified"] is True
    assert projection["revocation_checked"] is True
    assert projection["authority_state"]["generation"] == 1
    assert projection["trusted_time_source_verified"] is False
    assert projection["full_rollback_resistance_verified"] is False
    assert projection["authority_state"]["hardware_key_custody_verified"] is False

    missing_projection, missing_reason = verify_recovery_authority_grant(
        material["grant"],
        material["trust"],
        verification_time_ms=NOW_MS + 2,
    )
    assert missing_projection is None
    assert missing_reason == "recovery_authority_current_state_required"

    historical, historical_reason = verify_recovery_authority_grant(
        material["grant"],
        material["trust"],
        verification_time_ms=NOW_MS + 2,
        allow_embedded_authority_state=True,
    )
    assert historical_reason is None
    assert historical is not None


def test_newer_root_signed_state_revokes_grant_and_operator_certificate(
    tmp_path: Path,
):
    material = _materials(tmp_path)
    state_two = issue_recovery_authority_state(
        material["trust"],
        material["root_key"],
        material["custody"],
        generation=2,
        previous_state_sha256=material["state"]["state_sha256"],
        revoked_grant_sha256s=[material["grant"]["grant_sha256"]],
        issued_at_ms=NOW_MS + 100,
        valid_until_ms=NOW_MS + 30_000,
        minimum_accepted_time_ms=NOW_MS + 100,
    )
    anchor_two = issue_recovery_rollback_anchor(
        material["trust"],
        material["root_key"],
        state_two,
        previous_anchor_sha256=material["anchor"]["anchor_sha256"],
    )

    assert verify_recovery_authority_grant(
        material["grant"],
        material["trust"],
        verification_time_ms=NOW_MS + 101,
        authority_state=state_two,
        rollback_anchor=anchor_two,
    )[1] == "recovery_authority_grant_revoked"

    certificate_state = issue_recovery_authority_state(
        material["trust"],
        material["root_key"],
        material["custody"],
        generation=2,
        previous_state_sha256=material["state"]["state_sha256"],
        revoked_operator_certificate_sha256s=[
            material["certificate"]["certificate_sha256"]
        ],
        issued_at_ms=NOW_MS + 100,
        valid_until_ms=NOW_MS + 30_000,
        minimum_accepted_time_ms=NOW_MS + 100,
    )
    certificate_anchor = issue_recovery_rollback_anchor(
        material["trust"],
        material["root_key"],
        certificate_state,
        previous_anchor_sha256=material["anchor"]["anchor_sha256"],
    )
    assert verify_recovery_authority_grant(
        material["grant"],
        material["trust"],
        verification_time_ms=NOW_MS + 101,
        authority_state=certificate_state,
        rollback_anchor=certificate_anchor,
    )[1] == "recovery_operator_certificate_revoked"


def test_clock_rollback_and_surface_rewrites_fail_closed(tmp_path: Path):
    material = _materials(tmp_path)

    assert verify_recovery_authority_state(
        material["state"],
        material["trust"],
        rollback_anchor=material["anchor"],
        verification_time_ms=NOW_MS - 1,
    )[1] == "recovery_authority_clock_rollback_detected"

    tampered_state = deepcopy(material["state"])
    tampered_state["revoked_grant_sha256s"] = ["9" * 64]
    assert verify_recovery_authority_state(
        tampered_state,
        material["trust"],
        rollback_anchor=material["anchor"],
        verification_time_ms=NOW_MS + 1,
    )[1] == "recovery_authority_state_signature_invalid"

    tampered_anchor = deepcopy(material["anchor"])
    tampered_anchor["authority_state_sha256"] = "8" * 64
    assert verify_recovery_authority_state(
        material["state"],
        material["trust"],
        rollback_anchor=tampered_anchor,
        verification_time_ms=NOW_MS + 1,
    )[1] == "recovery_authority_rollback_anchor_signature_invalid"


def test_grant_bound_to_new_generation_rejects_older_external_state(tmp_path: Path):
    material = _materials(tmp_path)
    state_two = issue_recovery_authority_state(
        material["trust"],
        material["root_key"],
        material["custody"],
        generation=2,
        previous_state_sha256=material["state"]["state_sha256"],
        issued_at_ms=NOW_MS + 100,
        valid_until_ms=NOW_MS + 30_000,
        minimum_accepted_time_ms=NOW_MS + 100,
    )
    anchor_two = issue_recovery_rollback_anchor(
        material["trust"],
        material["root_key"],
        state_two,
        previous_anchor_sha256=material["anchor"]["anchor_sha256"],
    )
    grant_two = issue_recovery_authority_grant(
        material["trust"],
        material["certificate"],
        material["operator_key"],
        material["supervisor_public"],
        program_id="program-arch12",
        program_sha256="1" * 64,
        attempt_id="attempt-arch12",
        provider_instance_sha256="2" * 64,
        lease_resource_id="3" * 64,
        lease_owner_id="owner-arch12",
        transport_recovery_authority_sha256="4" * 64,
        context_intervention_sha256="5" * 64,
        issued_at_ms=NOW_MS + 101,
        expires_at_ms=NOW_MS + 20_000,
        nonce="grant-arch12-generation-two",
        authority_state=state_two,
        rollback_anchor=anchor_two,
    )

    assert verify_recovery_authority_grant(
        grant_two,
        material["trust"],
        verification_time_ms=NOW_MS + 102,
        authority_state=material["state"],
        rollback_anchor=material["anchor"],
    )[1] == "recovery_authority_state_generation_rollback"


def test_unattested_local_custody_cannot_satisfy_hardware_policy(tmp_path: Path):
    policy = {
        "schema": RECOVERY_KEY_CUSTODY_POLICY_SCHEMA,
        "allowed_providers": ["local_file"],
        "require_os_protection": False,
        "require_hardware_backing": True,
        "require_external_attestation": True,
    }
    root_key = tmp_path / "root.pem"
    operator_key = tmp_path / "operator.pem"
    trust, _ = generate_recovery_authority_pki(
        root_key,
        operator_key,
        operator_id="operator-hardware-policy",
        valid_from_ms=NOW_MS - 1_000,
        valid_until_ms=NOW_MS + 60_000,
        authority_state_required=True,
        key_custody_policy=policy,
    )
    custody = build_recovery_key_custody_evidence(trust["root_key_id"])
    state = issue_recovery_authority_state(
        trust,
        root_key,
        custody,
        generation=1,
        issued_at_ms=NOW_MS,
        valid_until_ms=NOW_MS + 30_000,
    )

    projection, reason = verify_recovery_authority_state(
        state,
        trust,
        rollback_anchor=None,
        verification_time_ms=NOW_MS + 1,
        require_rollback_anchor=False,
    )
    assert projection is None
    assert reason == "recovery_key_hardware_custody_unverified"

    with pytest.raises(
        RecoveryAuthorityStateError,
        match="recovery_key_hardware_custody_unverified",
    ):
        issue_recovery_rollback_anchor(trust, root_key, state)


def test_provider_lease_requires_live_state_and_read_only_status_preserves_bytes(
    tmp_path: Path,
):
    live_now_ms = time.time_ns() // 1_000_000
    material = _materials(tmp_path / "authority", now_ms=live_now_ms)
    provider_db = tmp_path / "provider.db"
    provider_key = tmp_path / "provider.key"
    store = DurableProcessProviderStore(
        provider_db,
        authentication_key_file=provider_key,
        recovery_authority_trust_anchor=material["trust"],
        recovery_authority_state=material["state"],
        recovery_authority_rollback_anchor=material["anchor"],
    )
    provider_instance = store.identity()
    resource_id = build_recovery_lease_resource_id(
        program_id="program-arch12-store",
        attempt_id="attempt-arch12-store",
        provider_instance_sha256=provider_instance,
    )
    grant = issue_recovery_authority_grant(
        material["trust"],
        material["certificate"],
        material["operator_key"],
        material["supervisor_public"],
        program_id="program-arch12-store",
        program_sha256="6" * 64,
        attempt_id="attempt-arch12-store",
        provider_instance_sha256=provider_instance,
        lease_resource_id=resource_id,
        lease_owner_id="owner-arch12-store",
        transport_recovery_authority_sha256="7" * 64,
        context_intervention_sha256="8" * 64,
        issued_at_ms=live_now_ms,
        expires_at_ms=live_now_ms + 20_000,
        nonce="grant-arch12-store",
        authority_state=material["state"],
        rollback_anchor=material["anchor"],
    )

    lease = store.acquire_recovery_lease(
        resource_id,
        "owner-arch12-store",
        ttl_ms=1_000,
        adoption_authority=grant,
    )
    store.release_recovery_lease(
        resource_id,
        "owner-arch12-store",
        generation=lease["generation"],
        lease_token=lease["lease_token"],
    )

    missing_state_store = DurableProcessProviderStore(
        provider_db,
        authentication_key_file=provider_key,
        recovery_authority_trust_anchor=material["trust"],
    )
    with pytest.raises(
        ProcessTransportError,
        match="recovery_authority_current_state_required",
    ):
        missing_state_store.acquire_recovery_lease(
            resource_id,
            "owner-arch12-store",
            ttl_ms=1_000,
            adoption_authority=grant,
        )

    before = hashlib.sha256(provider_db.read_bytes()).hexdigest()
    read_only = DurableProcessProviderStore(
        provider_db,
        authentication_key_file=provider_key,
        recovery_authority_trust_anchor=material["trust"],
        recovery_authority_state=material["state"],
        recovery_authority_rollback_anchor=material["anchor"],
        read_only=True,
    )
    status = read_only.status()
    read_only.recovery_lease_status(resource_id)
    after = hashlib.sha256(provider_db.read_bytes()).hexdigest()

    assert status["recovery_authority_state_configured"] is True
    assert status["recovery_authority_state_sha256"] == material["state"][
        "state_sha256"
    ]
    assert status["full_rollback_resistance_verified"] is False
    assert before == after
