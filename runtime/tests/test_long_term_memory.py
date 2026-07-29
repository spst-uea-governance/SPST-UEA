import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from spst_runtime.memory.long_term_memory import (
    CURRENT_MEMORY_POLICY_VERSION,
    LongTermMemoryStore,
    _matched_relevance_terms,
    _normalize_relevance_text,
    _weighted_relevance_terms,
)


@pytest.mark.parametrize(
    ("query", "record"),
    [
        ("repository identity Receipt", "Receipt binds repository identity."),
        ("foo-bar abcdef0", "prefix/foo-bar suffix abcdef0"),
        ("foo-bar", "prefix.foo-bar.suffix"),
        ("証拠 実行 設定", "証拠を実行設定へ結び付ける"),
        ("ab cd", "x.ab-y cdz"),
        ("receipt", "unrelated evidence"),
        ("a an the", "repository evidence"),
        ("path/to/file.py", "path/to/file.py and path/to/other.py"),
    ],
)
def test_query_projected_term_matching_preserves_full_lexicon_intersection(
    query: str,
    record: str,
) -> None:
    normalized_query = _normalize_relevance_text(query)
    normalized_record = _normalize_relevance_text(record)
    query_terms = _weighted_relevance_terms(normalized_query)
    record_terms = _weighted_relevance_terms(normalized_record)

    matched, record_has_matched_terms = _matched_relevance_terms(
        normalized_record,
        query_terms.keys(),
    )

    expected = set(query_terms) & set(record_terms)
    assert matched == expected
    assert record_has_matched_terms is bool(expected)


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


def test_current_policy_quarantines_legacy_from_all_reuse_paths(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    legacy = store.remember(
        "Legacy deterministic verifier guidance.",
        salience=0.95,
        confidence=0.9,
        policy_version=None,
    )
    obsolete = store.remember(
        "Obsolete deterministic verifier guidance.",
        salience=0.95,
        confidence=0.9,
        policy_version="obsolete-policy-v0",
    )
    current = store.remember(
        "Current deterministic verifier guidance.",
        salience=0.95,
        confidence=0.9,
        policy_version=CURRENT_MEMORY_POLICY_VERSION,
    )

    current_results = store.search("deterministic verifier", limit=10)
    audit_results = store.search(
        "legacy deterministic verifier",
        policy_version=None,
        limit=10,
    )
    health = store.retrieval_health()
    consolidated = store.consolidate()

    assert [item["id"] for item in current_results] == [current.id]
    assert audit_results[0]["id"] == legacy.id
    assert audit_results[0]["retrieval"]["policy_status"] == "not_required"
    assert health["eligible_records"] == 1
    assert health["quarantined_records"] == 2
    assert health["reason_counts"]["policy_version_missing"] == 1
    assert health["reason_counts"]["policy_mismatch"] == 1
    assert consolidated["eligible_records"] == 1
    assert consolidated["ineligible_active_records"] == 2
    assert [item["id"] for item in consolidated["high_salience"]] == [current.id]

    crystals = store.crystallize_rules(limit=5)
    records = {record.id: record for record in store.list_all()}

    assert len(crystals) == 1
    assert crystals[0]["metadata"]["source_record_id"] == current.id
    assert records[legacy.id].status == "active"
    assert records[obsolete.id].status == "active"
    assert records[current.id].status == "retired"


def test_memory_policy_boundaries_fail_closed_on_invalid_controls(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))

    with pytest.raises(ValueError, match="confidence threshold"):
        store.search("anything", min_confidence=-0.01)
    with pytest.raises(ValueError, match="confidence threshold"):
        store.retrieval_health(min_confidence=1.01)
    with pytest.raises(ValueError, match="cannot be negative"):
        store.crystallize_rules(limit=-1)
    with pytest.raises(ValueError, match="policy version cannot be empty"):
        store.remember("invalid policy", policy_version="")
    with pytest.raises(ValueError, match="ISO-8601"):
        store.remember("invalid expiry", expires_at="not-a-datetime")


def test_zero_crystal_limit_is_noop_and_corrupt_legacy_expiry_is_quarantined(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    current = store.remember(
        "Current deterministic verifier remains untouched.",
        salience=0.95,
        confidence=0.9,
    )
    corrupt = store.remember(
        "Corrupt expiry must not stop healthy retrieval.",
        confidence=0.9,
    )
    corrupt_payload = corrupt.as_dict()
    corrupt_payload["expires_at"] = "not-a-datetime"
    asyncio.run(
        store.repository.save(
            f"long_term_memory:record:{corrupt.id}",
            corrupt_payload,
        )
    )

    assert store.crystallize_rules(limit=0) == []
    assert {record.id for record in store.list_active()} == {current.id}
    assert [item["id"] for item in store.search("deterministic verifier")] == [current.id]
    assert corrupt.id not in {item["id"] for item in store.search("corrupt expiry")}
    assert store.retrieval_health()["reason_counts"]["invalid_expiry"] == 1
    assert store.expire_stale()["invalid_expiry_ids"] == [corrupt.id]
    assert {record.id: record.status for record in store.list_all()} == {
        current.id: "active",
        corrupt.id: "active",
    }


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


def test_relevance_gate_rejects_unrelated_query_and_preserves_supported_identifiers(tmp_path):
    store = LongTermMemoryStore(str(tmp_path / "memory.db"))
    english = store.remember(
        "Model input binding preserves the context packet digest for repository verification.",
        confidence=0.95,
    )
    japanese = store.remember(
        "リポジトリ検証では文脈パケットのダイジェストをモデル入力へ結び付ける。",
        confidence=0.95,
    )
    path_record = store.remember(
        "Inspect runtime/spst_runtime/context_mediation.py at commit "
        "95a33a9630e23970d2316cac297c1860aa67a525.",
        confidence=0.95,
    )
    store.remember(
        "Merge GitHub pull request into master and verify Runtime CI.",
        confidence=0.95,
    )

    assert store.search("orchid photosynthesis marine geology") == []
    assert store.search("context packet digest repository verification")[0]["id"] == english.id
    assert store.search("文脈パケット ダイジェスト モデル入力")[0]["id"] == japanese.id
    assert store.search("runtime/spst_runtime/context_mediation.py")[0]["id"] == path_record.id
    commit_result = store.search("95a33a9630e23970d2316cac297c1860aa67a525")[0]
    assert commit_result["id"] == path_record.id
    assert commit_result["relevance"]["profile"] == "deterministic-lexical-v2"
    assert commit_result["relevance"]["eligible"] is True
