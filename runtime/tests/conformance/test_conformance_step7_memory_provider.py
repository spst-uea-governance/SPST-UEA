import pytest

from spst_runtime.providers.memory_provider import MemoryProvider


@pytest.mark.conformance
def test_conformance_step7_memory_provider_store_retrieve_search(tmp_path):
    provider = MemoryProvider(path=str(tmp_path / "memory.db"))

    provider.store(
        "turn:1",
        {"text": "alpha memory", "score": 1},
        {"event": "chat_turn", "phase": "commit"},
    )
    provider.store(
        "turn:2",
        {"text": "beta memory", "score": 2},
        {"event": "chat_turn", "phase": "commit"},
    )

    restored = provider.retrieve("turn:1")
    results = provider.search("alpha", top_k=5)

    assert restored == {
        "key": "turn:1",
        "value": {"score": 1, "text": "alpha memory"},
        "metadata": {"event": "chat_turn", "phase": "commit"},
    }
    assert [item["key"] for item in results] == ["turn:1"]


@pytest.mark.conformance
def test_conformance_step7_memory_provider_is_deterministic_and_healthy(tmp_path):
    provider = MemoryProvider(path=str(tmp_path / "memory.db"))
    provider.store("b", {"text": "shared term second"}, {"order": 2})
    provider.store("a", {"text": "shared term first"}, {"order": 1})

    first = provider.search("shared term", top_k=10)
    second = provider.search("shared term", top_k=10)

    assert provider.health()["ok"] is True
    assert [item["key"] for item in first] == ["a", "b"]
    assert first == second
