import hashlib
from datetime import datetime, timedelta, timezone

from spst_runtime.memory.long_term_memory import (
    CURRENT_MEMORY_POLICY_VERSION,
    LongTermMemoryStore,
)


def test_long_term_memory_remember_search_and_stats(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    record = store.remember(
        "SPST-UEA keeps long term memory",
        tags=["memory", "spst"],
        salience=0.9,
        created_turn=1,
    )

    assert record.id
    assert store.stats()["total_records"] == 1
    results = store.search("long term memory")
    assert results[0]["id"] == record.id
    assert store.consolidate()["high_salience_count"] == 1


def test_long_term_memory_deduplicates_same_text(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    first = store.remember("same memory", created_turn=1)
    second = store.remember("same memory", created_turn=2)

    assert first.id == second.id
    assert store.stats()["total_records"] == 1
    assert store.list_all()[0].metadata["seen_count"] == 2


def test_search_rejects_expired_low_confidence_and_policy_mismatched_records(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    expired = store.remember(
        "shared durable guidance expired",
        confidence=0.9,
        expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    low_confidence = store.remember(
        "shared durable guidance uncertain",
        confidence=0.2,
    )
    wrong_policy = store.remember(
        "shared durable guidance old policy",
        confidence=0.9,
        policy_version="obsolete-policy-v0",
    )
    current = store.remember(
        "shared durable guidance current",
        source="verified_test",
        confidence=0.95,
        policy_version=CURRENT_MEMORY_POLICY_VERSION,
    )

    results = store.search(
        "shared durable guidance",
        now=now,
        min_confidence=0.5,
        policy_version=CURRENT_MEMORY_POLICY_VERSION,
        limit=10,
    )

    assert [item["id"] for item in results] == [current.id]
    assert results[0]["retrieval"]["source"] == "verified_test"
    assert results[0]["retrieval"]["policy_status"] == "current"
    assert expired.id not in {item["id"] for item in results}
    assert low_confidence.id not in {item["id"] for item in results}
    assert wrong_policy.id not in {item["id"] for item in results}


def test_retirement_preserves_audit_record_but_removes_retrieval(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    record = store.remember("retire this stale instruction", confidence=0.9)

    retired = store.retire(record.id, reason="superseded_by_verified_policy")

    assert retired.status == "retired"
    assert retired.metadata["retirement_reason"] == "superseded_by_verified_policy"
    assert store.search("stale instruction") == []
    assert store.list_all()[0].id == record.id
    assert store.stats()["by_status"]["retired"] == 1
    assert store.repository.verify_provenance()["valid"] is True


def test_expiration_and_rule_crystal_distillation_are_auditable(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    expired = store.remember(
        "temporary implementation detail",
        confidence=0.8,
        expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    source = store.remember(
        "Unknown tasks require a deterministic verifier before commit.",
        salience=0.95,
        confidence=0.9,
    )

    expiration = store.expire_stale(now=now)
    crystals = store.crystallize_rules(limit=1)

    records = {record.id: record for record in store.list_all()}
    assert expiration["retired_ids"] == [expired.id]
    assert crystals[0]["kind"] == "rule_crystal"
    assert records[source.id].status == "retired"
    assert records[source.id].superseded_by == crystals[0]["id"]
    assert any(item["kind"] == "rule_crystal" for item in store.search("deterministic verifier"))


def test_reopening_populated_memory_does_not_reindex_or_change_database(tmp_path):
    path = tmp_path / "memory.db"
    store = LongTermMemoryStore(str(path))
    store.remember("stable indexed memory", confidence=0.9)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    reopened = LongTermMemoryStore(str(path))

    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert after == before
    assert reopened.search("stable indexed memory")[0]["text"] == "stable indexed memory"
