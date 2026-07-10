from spst_runtime.storage.memory_store import InMemoryStore
def test_store():
    s=InMemoryStore()
    s.put("a",1)
    assert s.get("a")==1
