import asyncio
import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from spst_runtime.persistence.sqlite_repository import SQLiteRepository


INDEX_KEY = "long_term_memory:index"
FINGERPRINT_KEY = "long_term_memory:fingerprints"
SEARCH_INDEX_TABLE = "long_term_memory_search_index"


@dataclass
class MemoryRecord:
    id: str
    text: str
    kind: str = "episodic"
    source: str = "codex_chat"
    tags: list[str] = field(default_factory=list)
    salience: float = 0.5
    created_turn: int = 0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LongTermMemoryStore:
    """Persistent searchable memory for Codex-mediated SPST-UEA sessions."""

    def __init__(self, path: str | None = None):
        default_path = Path(__file__).resolve().parents[2] / "spst_long_term_memory.db"
        self.repository = SQLiteRepository(path or str(default_path))
        self._ensure_search_index()

    def remember(
        self,
        text: str,
        *,
        kind: str = "episodic",
        source: str = "codex_chat",
        tags: list[str] | None = None,
        salience: float = 0.5,
        created_turn: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        with self.repository.locked():
            normalized = self._normalize_text(text)
            fingerprint = self._fingerprint(normalized, kind, source)
            fingerprints = self._load_fingerprints()
            existing_id = fingerprints.get(fingerprint)
            if existing_id:
                existing_data = asyncio.run(self.repository.load(self._record_key(existing_id)))
                if existing_data:
                    record = MemoryRecord(**existing_data)
                    record.salience = max(record.salience, salience)
                    record.tags = sorted(set(record.tags) | set(tags or []))
                    record.metadata["last_seen_turn"] = created_turn
                    record.metadata["seen_count"] = int(record.metadata.get("seen_count", 1)) + 1
                    asyncio.run(self.repository.save(self._record_key(record.id), record.as_dict()))
                    self._index_record(record)
                    return record

            record = MemoryRecord(
                id=self._make_id(normalized, kind, created_turn),
                text=normalized,
                kind=kind,
                source=source,
                tags=tags or [],
                salience=salience,
                created_turn=created_turn,
                metadata=metadata or {},
            )
            asyncio.run(self.repository.save(self._record_key(record.id), record.as_dict()))
            self._index_record(record)
            index = self._load_index()
            if record.id not in index:
                index.append(record.id)
                asyncio.run(self.repository.save(INDEX_KEY, {"ids": index}))
            fingerprints[fingerprint] = record.id
            asyncio.run(self.repository.save(FINGERPRINT_KEY, fingerprints))
            return record

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query_terms = self._terms(query)
        scored = []
        for record in self._search_candidates(query_terms):
            score = self._score(query_terms, record)
            if score > 0:
                payload = record.as_dict()
                payload["score"] = score
                scored.append(payload)
        scored.sort(key=lambda item: (item["score"], item["salience"], item["created_turn"]), reverse=True)
        return scored[:limit]

    def list_recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        records = sorted(self.list_all(), key=lambda record: record.created_turn, reverse=True)
        return [record.as_dict() for record in records[:limit]]

    def list_all(self) -> list[MemoryRecord]:
        records = []
        for memory_id in self._load_index():
            data = asyncio.run(self.repository.load(self._record_key(memory_id)))
            if data:
                records.append(MemoryRecord(**data))
        return records

    def compact(self) -> dict[str, Any]:
        records = self.list_all()
        unique: dict[str, MemoryRecord] = {}
        removed_ids = []
        for record in records:
            fingerprint = self._fingerprint(record.text, record.kind, record.source)
            existing = unique.get(fingerprint)
            if existing is None:
                unique[fingerprint] = record
                continue
            existing.salience = max(existing.salience, record.salience)
            existing.tags = sorted(set(existing.tags) | set(record.tags))
            existing.metadata["seen_count"] = int(existing.metadata.get("seen_count", 1)) + int(
                record.metadata.get("seen_count", 1)
            )
            existing.metadata["last_seen_turn"] = max(
                int(existing.metadata.get("last_seen_turn", existing.created_turn)),
                int(record.metadata.get("last_seen_turn", record.created_turn)),
            )
            removed_ids.append(record.id)

        new_ids = [record.id for record in unique.values()]
        fingerprints = {
            self._fingerprint(record.text, record.kind, record.source): record.id
            for record in unique.values()
        }
        for record in unique.values():
            asyncio.run(self.repository.save(self._record_key(record.id), record.as_dict()))
        asyncio.run(self.repository.save(INDEX_KEY, {"ids": new_ids}))
        asyncio.run(self.repository.save(FINGERPRINT_KEY, fingerprints))
        return {
            "before": len(records),
            "after": len(new_ids),
            "removed_duplicate_ids": removed_ids,
        }

    def consolidate(self) -> dict[str, Any]:
        records = self.list_all()
        durable_tags = sorted({tag for record in records for tag in record.tags})
        high_salience = [record.as_dict() for record in records if record.salience >= 0.8]
        return {
            "total_records": len(records),
            "durable_tags": durable_tags,
            "high_salience_count": len(high_salience),
            "high_salience": high_salience[-10:],
        }

    def crystallize_rules(self, *, limit: int = 5) -> list[dict[str, Any]]:
        records = [
            record for record in self.list_all()
            if record.kind != "rule_crystal" and record.salience >= 0.8
        ]
        crystals = []
        for record in records[-limit:]:
            rule_text = self._abstract_rule(record)
            crystal = self.remember(
                rule_text,
                kind="rule_crystal",
                source="memory_distillation",
                tags=sorted(set(record.tags) | {"rule_crystal", "distilled"}),
                salience=0.95,
                created_turn=record.created_turn,
                metadata={
                    "source_record_id": record.id,
                    "distillation": "cmi_rule_crystal",
                },
            )
            crystals.append(crystal.as_dict())
        return crystals

    def stats(self) -> dict[str, Any]:
        records = self.list_all()
        by_kind: dict[str, int] = {}
        for record in records:
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        return {
            "total_records": len(records),
            "by_kind": by_kind,
            "latest_turn": max((record.created_turn for record in records), default=0),
        }

    def search_index_stats(self) -> dict[str, int]:
        with self.repository.connection() as conn:
            indexed_records = conn.execute(
                f"SELECT COUNT(DISTINCT memory_id) FROM {SEARCH_INDEX_TABLE}"
            ).fetchone()[0]
            terms = conn.execute(f"SELECT COUNT(DISTINCT term) FROM {SEARCH_INDEX_TABLE}").fetchone()[0]
        return {"indexed_records": int(indexed_records), "terms": int(terms)}

    def _load_index(self) -> list[str]:
        data = asyncio.run(self.repository.load(INDEX_KEY))
        return list(data.get("ids", [])) if data else []

    def _load_fingerprints(self) -> dict[str, str]:
        data = asyncio.run(self.repository.load(FINGERPRINT_KEY))
        return dict(data) if data else {}

    def _ensure_search_index(self) -> None:
        with self.repository.locked(), self.repository.connection() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SEARCH_INDEX_TABLE} (
                    term TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    PRIMARY KEY(term, memory_id)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_long_term_memory_search_lookup
                ON {SEARCH_INDEX_TABLE}(term, memory_id)
                """
            )
        for record in self.list_all():
            self._index_record(record)

    def _index_record(self, record: MemoryRecord) -> None:
        terms = self._terms(" ".join([record.text, " ".join(record.tags), record.kind, record.source]))
        with self.repository.connection() as conn:
            conn.execute(f"DELETE FROM {SEARCH_INDEX_TABLE} WHERE memory_id = ?", (record.id,))
            conn.executemany(
                f"INSERT OR IGNORE INTO {SEARCH_INDEX_TABLE}(term, memory_id) VALUES (?, ?)",
                [(term, record.id) for term in terms],
            )

    def _search_candidates(self, query_terms: set[str]) -> list[MemoryRecord]:
        if not query_terms:
            return []
        placeholders = ", ".join("?" for _ in query_terms)
        with self.repository.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT memory_id
                FROM {SEARCH_INDEX_TABLE}
                WHERE term IN ({placeholders})
                GROUP BY memory_id
                ORDER BY COUNT(*) DESC, memory_id ASC
                """,
                tuple(sorted(query_terms)),
            ).fetchall()
        records = []
        for (memory_id,) in rows:
            data = asyncio.run(self.repository.load(self._record_key(memory_id)))
            if data:
                records.append(MemoryRecord(**data))
        return records

    def _record_key(self, memory_id: str) -> str:
        return f"long_term_memory:record:{memory_id}"

    def _make_id(self, text: str, kind: str, created_turn: int) -> str:
        digest = hashlib.sha256(f"{kind}:{created_turn}:{text}".encode("utf-8")).hexdigest()
        return digest[:16]

    def _fingerprint(self, text: str, kind: str, source: str) -> str:
        return hashlib.sha256(f"{kind}:{source}:{text}".encode("utf-8")).hexdigest()[:24]

    def _normalize_text(self, text: str) -> str:
        return " ".join(text.strip().split())

    def _terms(self, text: str) -> set[str]:
        lowered = text.lower()
        words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
        chars = {lowered[index : index + 2] for index in range(max(len(lowered) - 1, 0))}
        return {term for term in words | chars if term.strip()}

    def _score(self, query_terms: set[str], record: MemoryRecord) -> float:
        record_terms = self._terms(" ".join([record.text, " ".join(record.tags), record.kind, record.source]))
        if not query_terms or not record_terms:
            return 0.0
        overlap = len(query_terms & record_terms)
        if overlap == 0:
            return 0.0
        return overlap / max(len(query_terms), 1)

    def _abstract_rule(self, record: MemoryRecord) -> str:
        text = record.text.lower()
        if "deterministic" in text and "verifier" in text:
            return (
                "RuleCrystal: Unknown frontier tasks require a deterministic verifier "
                "before commit, with local sandbox validation and no external API dependency."
            )
        if "repair" in text or "low esi" in text:
            return (
                "RuleCrystal: Low-confidence transitions must pass governed self-repair "
                "before action or persistence."
            )
        return f"RuleCrystal: Preserve deterministic local governance for {record.kind} memory."
