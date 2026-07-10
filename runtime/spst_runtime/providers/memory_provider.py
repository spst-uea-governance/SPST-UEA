import asyncio
from dataclasses import dataclass, field
from typing import Any

from spst_runtime.interfaces.memory import Memory
from spst_runtime.memory.long_term_memory import LongTermMemoryStore, MemoryRecord
from spst_runtime.persistence.sqlite_repository import SQLiteRepository


@dataclass
class MemoryProvider(Memory):
    """SQLite-backed memory provider for retrieve and commit phases."""

    path: str = "spst_memory_provider.db"
    namespace: str = "memory"
    repository: SQLiteRepository = field(init=False)
    long_term_memory: LongTermMemoryStore = field(init=False)

    def __post_init__(self) -> None:
        self.repository = SQLiteRepository(self.path)
        self.long_term_memory = LongTermMemoryStore(self.path)

    def store(
        self,
        key: str,
        value: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "key": key,
            "value": value,
            "metadata": metadata or {},
        }
        self._run(self.repository.save(self._storage_key(key), record))
        self._distill_to_long_term(key, value, metadata or {})
        return record

    def retrieve(self, key: str) -> dict[str, Any] | None:
        return self._run(self.repository.load(self._storage_key(key)))

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        records = self._search_key_value(query)
        existing_keys = {record["key"] for record in records}
        for memory in self.search_long_term(query, top_k=top_k):
            memory_key = memory.get("metadata", {}).get("memory_provider_key")
            if memory_key:
                continue
            if memory_key in existing_keys:
                continue
            records.append(memory)
            existing_keys.add(memory["key"])
        return records[:top_k]

    def remember_long_term(
        self,
        text: str,
        *,
        tags: list[str] | None = None,
        salience: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        return self.long_term_memory.remember(
            text,
            tags=tags or [],
            salience=salience,
            metadata=metadata or {},
        )

    def search_long_term(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        results = []
        for item in self.long_term_memory.search(query, limit=top_k):
            normalized = dict(item)
            normalized.setdefault("key", f"long_term:{item.get('id')}")
            results.append(normalized)
        return results

    def _search_key_value(self, query: str) -> list[dict[str, Any]]:
        terms = self._terms(query)
        records = self._load_all()
        scored = []
        for record in records:
            haystack = self._search_text(record)
            score = sum(1 for term in terms if term in haystack)
            if score > 0 or not terms:
                scored.append((score, record["key"], record))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [record for _, _, record in scored]

    def health(self) -> dict[str, Any]:
        with self.repository.connect() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            conn.execute("SELECT 1 FROM state_store LIMIT 1").fetchone()
        return {
            "ok": True,
            "provider": "sqlite",
            "path": self.path,
            "journal_mode": journal_mode.lower(),
        }

    def _storage_key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def _load_all(self) -> list[dict[str, Any]]:
        prefix = f"{self.namespace}:"
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT value FROM state_store WHERE key LIKE ? ORDER BY key ASC",
                (f"{prefix}%",),
            ).fetchall()

        import json

        return [json.loads(row[0]) for row in rows]

    def _search_text(self, record: dict[str, Any]) -> str:
        value = record.get("value", {})
        metadata = record.get("metadata", {})
        return f"{record.get('key', '')} {value} {metadata}".lower()

    def _terms(self, query: str) -> list[str]:
        return [term for term in query.lower().split() if term]

    def _distill_to_long_term(
        self,
        key: str,
        value: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        text_parts = [
            str(value.get("prompt", "")),
            str(value.get("text", "")),
            " ".join(value.get("trace", [])) if isinstance(value.get("trace"), list) else "",
        ]
        text = " ".join(part for part in text_parts if part).strip()
        if not text:
            return

        phase = metadata.get("phase")
        salience = 0.85 if phase == "act" else 0.65
        tags = ["memory_provider", str(phase or "store")]
        self.long_term_memory.remember(
            text,
            tags=tags,
            salience=salience,
            created_turn=int(metadata.get("version", 0) or 0),
            metadata={**metadata, "memory_provider_key": key},
        )

    def _run(self, awaitable: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        raise RuntimeError("MemoryProvider sync API cannot run inside an active event loop.")
