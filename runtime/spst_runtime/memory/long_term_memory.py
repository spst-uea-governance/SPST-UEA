import asyncio
import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from spst_runtime.persistence.sqlite_repository import SQLiteRepository


INDEX_KEY = "long_term_memory:index"
FINGERPRINT_KEY = "long_term_memory:fingerprints"
SEARCH_INDEX_TABLE = "long_term_memory_search_index"
CURRENT_MEMORY_POLICY_VERSION = "spst-uea-covenant-v1"
ACTIVE_MEMORY_STATUS = "active"


@dataclass
class MemoryRecord:
    id: str
    text: str
    kind: str = "episodic"
    source: str = "codex_chat"
    tags: list[str] = field(default_factory=list)
    salience: float = 0.5
    created_turn: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence: float = 0.5
    policy_version: str | None = None
    expires_at: str | None = None
    status: str = ACTIVE_MEMORY_STATUS
    superseded_by: str | None = None
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
        confidence: float = 0.6,
        policy_version: str | None = CURRENT_MEMORY_POLICY_VERSION,
        ttl_seconds: int | None = None,
        expires_at: str | None = None,
        now: datetime | None = None,
    ) -> MemoryRecord:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Memory confidence must be between 0.0 and 1.0.")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("Memory TTL must be positive when provided.")
        reference_time = self._utc(now)
        resolved_expiry = expires_at
        if ttl_seconds is not None:
            resolved_expiry = (reference_time + timedelta(seconds=ttl_seconds)).isoformat()
        with self.repository.locked():
            normalized = self._normalize_text(text)
            fingerprint = self._fingerprint(normalized, kind, source)
            fingerprints = self._load_fingerprints()
            existing_id = fingerprints.get(fingerprint)
            if existing_id:
                existing_data = asyncio.run(self.repository.load(self._record_key(existing_id)))
                if existing_data:
                    record = MemoryRecord(**existing_data)
                    if record.status == ACTIVE_MEMORY_STATUS:
                        record.salience = max(record.salience, salience)
                        record.confidence = max(record.confidence, confidence)
                        record.tags = sorted(set(record.tags) | set(tags or []))
                        record.policy_version = policy_version or record.policy_version
                        if resolved_expiry is not None:
                            record.expires_at = resolved_expiry
                        record.metadata["last_seen_turn"] = created_turn
                        record.metadata["seen_count"] = int(
                            record.metadata.get("seen_count", 1)
                        ) + 1
                        asyncio.run(
                            self.repository.save(self._record_key(record.id), record.as_dict())
                        )
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
                confidence=confidence,
                policy_version=policy_version,
                expires_at=resolved_expiry,
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

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        now: datetime | None = None,
        min_confidence: float = 0.5,
        policy_version: str | None = CURRENT_MEMORY_POLICY_VERSION,
    ) -> list[dict[str, Any]]:
        query_terms = self._terms(query)
        scored = []
        reference_time = self._utc(now)
        for record in self._search_candidates(query_terms):
            eligible, policy_status = self._retrieval_eligibility(
                record,
                now=reference_time,
                min_confidence=min_confidence,
                policy_version=policy_version,
            )
            if not eligible:
                continue
            policy_factor = 0.75 if policy_status == "legacy_unversioned" else 1.0
            score = self._score(query_terms, record) * record.confidence * policy_factor
            if score > 0:
                payload = record.as_dict()
                payload["score"] = score
                payload["retrieval"] = {
                    "source": record.source,
                    "confidence": record.confidence,
                    "policy_status": policy_status,
                    "policy_version": record.policy_version,
                    "expires_at": record.expires_at,
                }
                scored.append(payload)
        scored.sort(
            key=lambda item: (item["score"], item["salience"], item["created_turn"]),
            reverse=True,
        )
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

    def list_active(self, *, now: datetime | None = None) -> list[MemoryRecord]:
        reference_time = self._utc(now)
        return [
            record
            for record in self.list_all()
            if record.status == ACTIVE_MEMORY_STATUS and not self._is_expired(record, reference_time)
        ]

    def retire(
        self,
        memory_id: str,
        *,
        reason: str,
        superseded_by: str | None = None,
    ) -> MemoryRecord:
        if not reason:
            raise ValueError("Memory retirement requires a reason.")
        data = asyncio.run(self.repository.load(self._record_key(memory_id)))
        if data is None:
            raise KeyError(f"Unknown memory record: {memory_id}")
        record = MemoryRecord(**data)
        record.status = "retired"
        record.superseded_by = superseded_by
        record.metadata["retirement_reason"] = reason
        record.metadata["retired_at"] = datetime.now(timezone.utc).isoformat()
        asyncio.run(self.repository.save(self._record_key(record.id), record.as_dict()))
        self._deindex_record(record.id)
        fingerprints = self._load_fingerprints()
        fingerprint = self._fingerprint(record.text, record.kind, record.source)
        if fingerprints.get(fingerprint) == record.id:
            fingerprints.pop(fingerprint)
            asyncio.run(self.repository.save(FINGERPRINT_KEY, fingerprints))
        return record

    def expire_stale(self, *, now: datetime | None = None) -> dict[str, Any]:
        reference_time = self._utc(now)
        retired_ids = []
        for record in self.list_all():
            if record.status == ACTIVE_MEMORY_STATUS and self._is_expired(record, reference_time):
                self.retire(record.id, reason="retention_expired")
                retired_ids.append(record.id)
        return {"retired_ids": retired_ids, "retired_count": len(retired_ids)}

    def compact(self) -> dict[str, Any]:
        records = self.list_all()
        unique: dict[str, MemoryRecord] = {}
        retired_ids = []
        for record in self.list_active():
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
            asyncio.run(self.repository.save(self._record_key(existing.id), existing.as_dict()))
            self.retire(
                record.id,
                reason="duplicate_compacted",
                superseded_by=existing.id,
            )
            retired_ids.append(record.id)

        fingerprints = {
            self._fingerprint(record.text, record.kind, record.source): record.id
            for record in unique.values()
        }
        asyncio.run(self.repository.save(FINGERPRINT_KEY, fingerprints))
        return {
            "before": len(records),
            "after": len(unique),
            "retired_duplicate_ids": retired_ids,
            "removed_duplicate_ids": retired_ids,
        }

    def consolidate(self) -> dict[str, Any]:
        records = self.list_active()
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
            record
            for record in self.list_active()
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
                confidence=max(record.confidence, 0.9),
                policy_version=CURRENT_MEMORY_POLICY_VERSION,
                metadata={
                    "source_record_id": record.id,
                    "distillation": "cmi_rule_crystal",
                },
            )
            self.retire(
                record.id,
                reason="distilled_to_rule_crystal",
                superseded_by=crystal.id,
            )
            crystals.append(crystal.as_dict())
        return crystals

    def stats(self) -> dict[str, Any]:
        records = self.list_all()
        by_kind: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for record in records:
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
            by_status[record.status] = by_status.get(record.status, 0) + 1
        return {
            "total_records": len(records),
            "by_kind": by_kind,
            "by_status": by_status,
            "active_records": by_status.get(ACTIVE_MEMORY_STATUS, 0),
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
            indexed = int(conn.execute(f"SELECT COUNT(*) FROM {SEARCH_INDEX_TABLE}").fetchone()[0])
        if indexed == 0:
            for record in self.list_active():
                self._index_record(record)

    def _index_record(self, record: MemoryRecord) -> None:
        self._deindex_record(record.id)
        if record.status != ACTIVE_MEMORY_STATUS:
            return
        terms = self._terms(
            " ".join([record.text, " ".join(record.tags), record.kind, record.source])
        )
        with self.repository.connection() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO {SEARCH_INDEX_TABLE}(term, memory_id) VALUES (?, ?)",
                [(term, record.id) for term in terms],
            )

    def _deindex_record(self, memory_id: str) -> None:
        with self.repository.connection() as conn:
            conn.execute(f"DELETE FROM {SEARCH_INDEX_TABLE} WHERE memory_id = ?", (memory_id,))

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

    def _retrieval_eligibility(
        self,
        record: MemoryRecord,
        *,
        now: datetime,
        min_confidence: float,
        policy_version: str | None,
    ) -> tuple[bool, str]:
        if record.status != ACTIVE_MEMORY_STATUS:
            return False, "retired"
        if self._is_expired(record, now):
            return False, "expired"
        if record.confidence < min_confidence:
            return False, "below_confidence_floor"
        if policy_version is None:
            return True, "not_required"
        if record.policy_version is None:
            return True, "legacy_unversioned"
        if record.policy_version != policy_version:
            return False, "policy_mismatch"
        return True, "current"

    def _is_expired(self, record: MemoryRecord, now: datetime) -> bool:
        if not record.expires_at:
            return False
        return self._parse_datetime(record.expires_at) <= now

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        resolved = value or datetime.now(timezone.utc)
        if resolved.tzinfo is None:
            return resolved.replace(tzinfo=timezone.utc)
        return resolved.astimezone(timezone.utc)

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

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
