from concurrent.futures import ThreadPoolExecutor

from spst_runtime.providers.memory_provider import MemoryProvider


def test_memory_provider_indexes_and_recovers_all_concurrent_writes(tmp_path):
    database_path = str(tmp_path / "concurrent_memory.db")

    def write_record(index: int) -> str:
        provider = MemoryProvider(path=database_path)
        key = f"record:{index:02d}"
        provider.store(
            key,
            {"text": f"concurrent indexed memory shared-token worker-{index}"},
            {"phase": "act", "worker": index},
        )
        return key

    with ThreadPoolExecutor(max_workers=12) as executor:
        keys = list(executor.map(write_record, range(24)))

    provider = MemoryProvider(path=database_path)
    results = provider.search("shared-token", top_k=30)
    health = provider.health()

    assert {item["key"] for item in results} == set(keys)
    assert health["search_index"]["indexed_records"] == 24
    assert health["search_index"]["terms"] > 0
    assert provider.long_term_memory.search_index_stats()["indexed_records"] == 24
