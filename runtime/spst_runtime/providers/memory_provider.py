import asyncio
import json
import re
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
        self._ensure_search_index()

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
        with self.repository.locked():
            self._run(self.repository.save(self._storage_key(key), record))
            self._index_record(record)
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

    def crystallize_rules(self, top_k: int = 5) -> list[dict[str, Any]]:
        return self.long_term_memory.crystallize_rules(limit=top_k)

    def _search_key_value(self, query: str) -> list[dict[str, Any]]:
        terms = self._terms(query)
        if not terms:
            return self._load_all()

        placeholders = ", ".join("?" for _ in terms)
        with self.repository.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT memory_key, COUNT(*) AS score
                FROM memory_search_index
                WHERE namespace = ? AND term IN ({placeholders})
                GROUP BY memory_key
                ORDER BY score DESC, memory_key ASC
                """,
                (self.namespace, *terms),
            ).fetchall()
        return [record for key, _ in rows if (record := self.retrieve(key)) is not None]

    def health(self) -> dict[str, Any]:
        with self.repository.connection() as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            conn.execute("SELECT 1 FROM state_store LIMIT 1").fetchone()
        return {
            "ok": True,
            "provider": "sqlite",
            "path": self.path,
            "journal_mode": journal_mode.lower(),
            "search_index": self._index_health(),
        }

    def _storage_key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def _load_all(self) -> list[dict[str, Any]]:
        prefix = f"{self.namespace}:"
        with self.repository.connection() as conn:
            rows = conn.execute(
                "SELECT value FROM state_store WHERE key LIKE ? ORDER BY key ASC",
                (f"{prefix}%",),
            ).fetchall()

        return [json.loads(row[0]) for row in rows]

    def _search_text(self, record: dict[str, Any]) -> str:
        value = record.get("value", {})
        metadata = record.get("metadata", {})
        return f"{record.get('key', '')} {value} {metadata}".lower()

    def _terms(self, query: str) -> list[str]:
        return sorted(set(re.findall(r"[a-z0-9_]+", query.lower())))

    def _ensure_search_index(self) -> None:
        with self.repository.locked(), self.repository.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_search_index (
                    namespace TEXT NOT NULL,
                    term TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    PRIMARY KEY(namespace, term, memory_key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_search_lookup
                ON memory_search_index(namespace, term, memory_key)
                """
            )
        for record in self._load_all():
            self._index_record(record)

    def _index_record(self, record: dict[str, Any]) -> None:
        terms = self._terms(self._search_text(record))
        with self.repository.connection() as conn:
            conn.execute(
                "DELETE FROM memory_search_index WHERE namespace = ? AND memory_key = ?",
                (self.namespace, record["key"]),
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO memory_search_index(namespace, term, memory_key)
                VALUES (?, ?, ?)
                """,
                [(self.namespace, term, record["key"]) for term in terms],
            )

    def _index_health(self) -> dict[str, int]:
        with self.repository.connection() as conn:
            indexed_records = conn.execute(
                "SELECT COUNT(DISTINCT memory_key) FROM memory_search_index WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()[0]
            terms = conn.execute(
                "SELECT COUNT(DISTINCT term) FROM memory_search_index WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()[0]
        return {"indexed_records": int(indexed_records), "terms": int(terms)}

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
