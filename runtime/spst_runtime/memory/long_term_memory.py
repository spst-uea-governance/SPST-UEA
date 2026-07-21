import asyncio
import hashlib
import json
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
MEMORY_RECORD_BINDING_SCHEMA = "spst-memory-record-binding-v1"
RELEVANCE_PROFILE = "deterministic-lexical-v2"
DEFAULT_MINIMUM_RELEVANCE = 0.35

_ENGLISH_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)
_LATIN_TOKEN = re.compile(r"[a-z0-9]+(?:[._:/\\-][a-z0-9]+)*")
_JAPANESE_RUN = re.compile(r"[ぁ-んァ-ヶー一-龯々]+")


def assess_context_relevance(
    query: str,
    record_text: str,
    *,
    minimum_relevance: float = DEFAULT_MINIMUM_RELEVANCE,
) -> dict[str, Any]:
    """Return a deterministic lexical relevance decision without trusting caller scores."""

    if not 0.0 <= minimum_relevance <= 1.0:
        raise ValueError("Memory relevance threshold must be between 0.0 and 1.0.")
    normalized_query = _normalize_relevance_text(query)
    normalized_record = _normalize_relevance_text(record_text)
    query_terms = _weighted_relevance_terms(normalized_query)
    record_terms = _weighted_relevance_terms(normalized_record)
    matched = set(query_terms) & set(record_terms)
    query_weight = sum(query_terms.values())
    matched_weight = sum(query_terms[term] for term in matched)
    coverage = matched_weight / query_weight if query_weight else 0.0
    match_strength = len(matched) / (len(matched) + 2.0) if matched else 0.0
    exact_phrase = bool(
        normalized_query
        and len(normalized_query.replace(" ", "")) >= 4
        and normalized_query in normalized_record
    )
    structured_match = any(_is_structured_term(term) for term in matched)
    score = min(1.0, (0.75 * coverage) + (0.25 * match_strength) + (0.1 if exact_phrase else 0.0))
    required_matches = 1 if len(query_terms) <= 1 or exact_phrase or structured_match else 2
    eligible = bool(
        query_terms
        and record_terms
        and len(matched) >= required_matches
        and score >= minimum_relevance
    )
    matched_digest = hashlib.sha256("\n".join(sorted(matched)).encode("utf-8")).hexdigest()
    return {
        "profile": RELEVANCE_PROFILE,
        "eligible": eligible,
        "score": score,
        "minimum_relevance": minimum_relevance,
        "query_term_count": len(query_terms),
        "matched_term_count": len(matched),
        "required_match_count": required_matches,
        "query_coverage": coverage,
        "exact_phrase": exact_phrase,
        "structured_match": structured_match,
        "matched_terms_sha256": matched_digest,
    }


def _normalize_relevance_text(text: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def _weighted_relevance_terms(text: str) -> dict[str, float]:
    terms: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        if term and term not in _ENGLISH_STOP_WORDS:
            terms[term] = max(terms.get(term, 0.0), weight)

    for match in _LATIN_TOKEN.finditer(text):
        token = match.group(0).strip("._:/\\-")
        if len(token) < 2:
            continue
        structured = _is_structured_term(token)
        add(token, 3.0 if structured else (1.5 if len(token) >= 8 else 1.0))
        if structured:
            for component in re.split(r"[._:/\\-]+", token):
                if len(component) >= 2:
                    add(component, 1.5 if len(component) >= 8 else 1.0)

    for run in _JAPANESE_RUN.findall(text):
        if len(run) < 2:
            continue
        add(run, 2.5)
        for width, weight in ((2, 0.5), (3, 1.0)):
            if len(run) < width:
                continue
            for index in range(len(run) - width + 1):
                add(run[index : index + width], weight)
    return terms


def _is_structured_term(term: str) -> bool:
    return bool(
        any(character in term for character in "._:/\\-")
        or (len(term) >= 7 and all(character in "0123456789abcdef" for character in term))
    )


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


def memory_record_binding_sha256(record: MemoryRecord | dict[str, Any]) -> str:
    """Digest every persisted record field while excluding retrieval-only fields."""

    source = record.as_dict() if isinstance(record, MemoryRecord) else record
    field_names = tuple(MemoryRecord.__dataclass_fields__)
    if not isinstance(source, dict) or any(field not in source for field in field_names):
        raise ValueError("Memory record binding requires every persisted field.")
    payload = {
        "schema": MEMORY_RECORD_BINDING_SCHEMA,
        "record": {field: source[field] for field in field_names},
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class LongTermMemoryStore:
    """Persistent searchable memory for Codex-mediated SPST-UEA sessions."""

    def __init__(self, path: str | None = None, *, read_only: bool = False):
        default_path = Path(__file__).resolve().parents[2] / "spst_long_term_memory.db"
        self.read_only = read_only
        self.repository = SQLiteRepository(path or str(default_path), read_only=read_only)
        if not read_only:
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
        self._validate_policy_version(policy_version)
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("Memory TTL must be positive when provided.")
        reference_time = self._utc(now)
        resolved_expiry = expires_at
        if ttl_seconds is not None:
            resolved_expiry = (reference_time + timedelta(seconds=ttl_seconds)).isoformat()
        if resolved_expiry is not None:
            try:
                self._parse_datetime(resolved_expiry)
            except ValueError as error:
                raise ValueError("Memory expires_at must be an ISO-8601 datetime.") from error
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
        min_relevance: float = DEFAULT_MINIMUM_RELEVANCE,
        policy_version: str | None = CURRENT_MEMORY_POLICY_VERSION,
    ) -> list[dict[str, Any]]:
        self._validate_retrieval_controls(
            min_confidence=min_confidence,
            min_relevance=min_relevance,
            policy_version=policy_version,
            limit=limit,
        )
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
            relevance = assess_context_relevance(
                query,
                record.text,
                minimum_relevance=min_relevance,
            )
            if relevance["eligible"]:
                score = float(relevance["score"]) * record.confidence
                payload = record.as_dict()
                payload["score"] = score
                payload["relevance"] = {
                    **relevance,
                    "retrieval_score": score,
                    "confidence_weighted": True,
                }
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
        records = []
        for record in self.list_all():
            if record.status != ACTIVE_MEMORY_STATUS:
                continue
            try:
                if not self._is_expired(record, reference_time):
                    records.append(record)
            except ValueError:
                continue
        return records

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
        invalid_expiry_ids = []
        for record in self.list_all():
            if record.status != ACTIVE_MEMORY_STATUS:
                continue
            try:
                expired = self._is_expired(record, reference_time)
            except ValueError:
                invalid_expiry_ids.append(record.id)
                continue
            if expired:
                self.retire(record.id, reason="retention_expired")
                retired_ids.append(record.id)
        return {
            "retired_ids": retired_ids,
            "retired_count": len(retired_ids),
            "invalid_expiry_ids": invalid_expiry_ids,
            "invalid_expiry_count": len(invalid_expiry_ids),
        }

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

    def retrieval_health(
        self,
        *,
        now: datetime | None = None,
        min_confidence: float = 0.5,
        policy_version: str | None = CURRENT_MEMORY_POLICY_VERSION,
    ) -> dict[str, Any]:
        """Report why stored records are eligible or quarantined from retrieval."""
        self._validate_retrieval_controls(
            min_confidence=min_confidence,
            policy_version=policy_version,
        )
        reference_time = self._utc(now)
        reason_counts: dict[str, int] = {}
        eligible_records = 0
        quarantined_records = 0
        for record in self.list_all():
            eligible, reason = self._retrieval_eligibility(
                record,
                now=reference_time,
                min_confidence=min_confidence,
                policy_version=policy_version,
            )
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if eligible:
                eligible_records += 1
            elif reason in {"policy_version_missing", "policy_mismatch"}:
                quarantined_records += 1
        total_records = sum(reason_counts.values())
        return {
            "policy_version": policy_version,
            "minimum_confidence": min_confidence,
            "total_records": total_records,
            "eligible_records": eligible_records,
            "ineligible_records": total_records - eligible_records,
            "quarantined_records": quarantined_records,
            "reason_counts": dict(sorted(reason_counts.items())),
        }

    def consolidate(
        self,
        *,
        now: datetime | None = None,
        min_confidence: float = 0.5,
        policy_version: str | None = CURRENT_MEMORY_POLICY_VERSION,
    ) -> dict[str, Any]:
        self._validate_retrieval_controls(
            min_confidence=min_confidence,
            policy_version=policy_version,
        )
        reference_time = self._utc(now)
        records = self.list_active(now=reference_time)
        eligible_records = [
            record
            for record in records
            if self._retrieval_eligibility(
                record,
                now=reference_time,
                min_confidence=min_confidence,
                policy_version=policy_version,
            )[0]
        ]
        durable_tags = sorted({tag for record in eligible_records for tag in record.tags})
        high_salience = [
            record.as_dict() for record in eligible_records if record.salience >= 0.8
        ]
        return {
            "total_records": len(records),
            "eligible_records": len(eligible_records),
            "ineligible_active_records": len(records) - len(eligible_records),
            "durable_tags": durable_tags,
            "high_salience_count": len(high_salience),
            "high_salience": high_salience[-10:],
            "retrieval_health": self.retrieval_health(
                now=reference_time,
                min_confidence=min_confidence,
                policy_version=policy_version,
            ),
        }

    def crystallize_rules(
        self,
        *,
        limit: int = 5,
        now: datetime | None = None,
        min_confidence: float = 0.5,
        policy_version: str | None = CURRENT_MEMORY_POLICY_VERSION,
    ) -> list[dict[str, Any]]:
        self._validate_retrieval_controls(
            min_confidence=min_confidence,
            policy_version=policy_version,
            limit=limit,
        )
        if limit == 0:
            return []
        reference_time = self._utc(now)
        records = [
            record
            for record in self.list_all()
            if record.kind != "rule_crystal"
            and record.salience >= 0.8
            and self._retrieval_eligibility(
                record,
                now=reference_time,
                min_confidence=min_confidence,
                policy_version=policy_version,
            )[0]
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
        return set(_weighted_relevance_terms(_normalize_relevance_text(text)))

    def _score(self, query_terms: set[str], record: MemoryRecord) -> float:
        record_terms = self._terms(record.text)
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
        try:
            expired = self._is_expired(record, now)
        except ValueError:
            return False, "invalid_expiry"
        if expired:
            return False, "expired"
        if not isinstance(record.confidence, (int, float)) or not 0.0 <= record.confidence <= 1.0:
            return False, "invalid_confidence"
        if record.confidence < min_confidence:
            return False, "below_confidence_floor"
        if policy_version is None:
            return True, "not_required"
        if record.policy_version is None:
            return False, "policy_version_missing"
        if record.policy_version != policy_version:
            return False, "policy_mismatch"
        return True, "current"

    def _validate_retrieval_controls(
        self,
        *,
        min_confidence: float,
        min_relevance: float = DEFAULT_MINIMUM_RELEVANCE,
        policy_version: str | None,
        limit: int | None = None,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("Memory confidence threshold must be between 0.0 and 1.0.")
        if not 0.0 <= min_relevance <= 1.0:
            raise ValueError("Memory relevance threshold must be between 0.0 and 1.0.")
        self._validate_policy_version(policy_version)
        if limit is not None and limit < 0:
            raise ValueError("Memory retrieval limit cannot be negative.")

    @staticmethod
    def _validate_policy_version(policy_version: str | None) -> None:
        if policy_version is not None and not policy_version.strip():
            raise ValueError("Memory policy version cannot be empty.")

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
