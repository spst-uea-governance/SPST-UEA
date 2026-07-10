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
        for record in self.list_all():
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

    def _load_index(self) -> list[str]:
        data = asyncio.run(self.repository.load(INDEX_KEY))
        return list(data.get("ids", [])) if data else []

    def _load_fingerprints(self) -> dict[str, str]:
        data = asyncio.run(self.repository.load(FINGERPRINT_KEY))
        return dict(data) if data else {}

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
