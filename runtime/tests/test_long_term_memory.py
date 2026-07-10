from spst_runtime.memory.long_term_memory import LongTermMemoryStore


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
