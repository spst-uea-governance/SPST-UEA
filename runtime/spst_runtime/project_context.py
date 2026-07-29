"""Verified, bounded retrieval from an owner-supplied ChatGPT Project snapshot."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from spst_runtime.memory.long_term_memory import assess_context_relevance


SNAPSHOT_SCHEMA = "chatgpt-project-export-v1"
CORPUS_SCHEMA = "spst-chatgpt-project-corpus-v1"
PACKET_SCHEMA = "spst-chatgpt-project-context-packet-v1"
CONTEXT_AUTHORITY = "untrusted_evidence_only"
DEFAULT_MAX_AGE_DAYS = 90
DEFAULT_MAX_ITEMS = 6
DEFAULT_MAX_CHARS = 8_000
SEGMENT_CHARS = 2_000

_PROJECT_ID = re.compile(r"^[0-9a-f]{32}$")
_CHAT_ID = re.compile(r"^[0-9a-f-]{36}$")
_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+|any\s+|the\s+)?previous\s+instructions\b", re.I),
    re.compile(r"\breveal\s+(?:the\s+)?system\s+prompt\b", re.I),
    re.compile(r"\b(?:system|developer)\s+message\s*:", re.I),
    re.compile(r"\b(?:override|bypass)\s+(?:the\s+)?(?:system|developer|safety)\b", re.I),
)


class DuplicateJSONKeyError(ValueError):
    """Raised when an input JSON object has an ambiguous duplicate key."""


def canonical_json(value: Any) -> bytes:
    """Serialize a value into the canonical byte representation used for hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_json(path: str | Path) -> dict[str, Any]:
    """Load strict JSON, rejecting duplicate keys and non-object roots."""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJSONKeyError(f"duplicate_json_key:{key}")
            result[key] = value
        return result

    loaded = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs_hook)
    if not isinstance(loaded, dict):
        raise ValueError("json_root_must_be_object")
    return loaded


def dump_json(path: str | Path, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def compile_snapshot(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Compile an explicit Project snapshot into a deterministic untrusted corpus."""

    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("snapshot_schema_invalid")
    if not 1 <= max_age_days <= 3650:
        raise ValueError("max_age_days_invalid")
    project = _compile_project(snapshot.get("project"))
    captured_at = _timestamp(snapshot.get("captured_at"), "captured_at_invalid")
    source_snapshot_sha256 = sha256_value(snapshot)
    chats = _compile_chats(snapshot.get("chats"), project)
    sources = _compile_sources(snapshot.get("sources", []), project)
    completeness = _compile_completeness(snapshot.get("completeness"), chats, sources)
    segments = _segments(chats, sources)
    compiled_at = _utc(now).isoformat().replace("+00:00", "Z")
    corpus: dict[str, Any] = {
        "schema": CORPUS_SCHEMA,
        "authority": CONTEXT_AUTHORITY,
        "source_authenticity": "owner_supplied_not_independently_verified",
        "project": project,
        "captured_at": captured_at,
        "compiled_at": compiled_at,
        "max_age_days": max_age_days,
        "source_snapshot_sha256": source_snapshot_sha256,
        "completeness": completeness,
        "counts": {
            "chats": len(chats),
            "messages": sum(len(chat["messages"]) for chat in chats),
            "sources": len(sources),
            "segments": len(segments),
        },
        "chats": chats,
        "sources": sources,
        "segments": segments,
    }
    corpus["corpus_sha256"] = sha256_value(corpus)
    return corpus


def verify_corpus(
    corpus: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify corpus integrity and freshness without changing state."""

    reason = _corpus_integrity_reason(corpus, now=now)
    return {
        "schema": "spst-chatgpt-project-corpus-status-v1",
        "status": "ready" if reason is None else "blocked",
        "reason": reason,
        "authority": CONTEXT_AUTHORITY,
        "corpus_sha256": corpus.get("corpus_sha256"),
        "project": corpus.get("project"),
        "counts": corpus.get("counts"),
    }


def query_corpus(
    corpus: dict[str, Any],
    query: str,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic, relevant, bounded packet from a verified corpus."""

    normalized_query = _normalize_text(query)
    if not normalized_query:
        raise ValueError("query_empty")
    if not 1 <= max_items <= 20:
        raise ValueError("max_items_invalid")
    if not 1 <= max_chars <= 32_000:
        raise ValueError("max_chars_invalid")

    status = verify_corpus(corpus, now=now)
    task_sha256 = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
    if status["status"] != "ready":
        return _packet(
            status="blocked",
            reason=status["reason"],
            corpus=corpus,
            task_sha256=task_sha256,
            items=[],
            candidate_count=0,
            selected_chars=0,
            rejection_reasons={str(status["reason"]): 1},
            max_items=max_items,
            max_chars=max_chars,
        )

    assessed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejected: dict[str, int] = {}
    segments = corpus.get("segments", [])
    for segment in segments:
        if segment.get("prompt_injection_suspected"):
            _count(rejected, "prompt_injection_suspected")
            continue
        relevance = assess_context_relevance(normalized_query, str(segment.get("text", "")))
        if not relevance["eligible"]:
            _count(rejected, "not_relevant")
            continue
        assessed.append((segment, relevance))

    assessed.sort(
        key=lambda item: (
            float(item[1]["score"]),
            str(item[0]["source_captured_at"]),
            str(item[0]["segment_id"]),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    selected_chars = 0
    fingerprints: set[str] = set()
    for segment, relevance in assessed:
        fingerprint = str(segment["text_sha256"])
        if fingerprint in fingerprints:
            _count(rejected, "duplicate_context")
            continue
        text = str(segment["text"])
        if len(selected) >= max_items:
            _count(rejected, "item_budget_exceeded")
            continue
        if selected_chars + len(text) > max_chars:
            _count(rejected, "character_budget_exceeded")
            continue
        fingerprints.add(fingerprint)
        selected_chars += len(text)
        selected.append(
            {
                "segment_id": segment["segment_id"],
                "kind": segment["kind"],
                "title": segment["title"],
                "text": text,
                "text_sha256": fingerprint,
                "source_url": segment["source_url"],
                "source_record_sha256": segment["source_record_sha256"],
                "source_captured_at": segment["source_captured_at"],
                "authority": CONTEXT_AUTHORITY,
                "relevance": relevance,
            }
        )
    packet_status = "ready" if selected else "empty"
    return _packet(
        status=packet_status,
        reason=None if selected else "no_eligible_context",
        corpus=corpus,
        task_sha256=task_sha256,
        items=selected,
        candidate_count=len(segments),
        selected_chars=selected_chars,
        rejection_reasons=rejected,
        max_items=max_items,
        max_chars=max_chars,
    )


def _compile_project(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("project_invalid")
    project_id = _required_string(value, "id")
    slug = _required_string(value, "slug")
    name = _required_string(value, "name")
    url = _required_string(value, "url")
    if not _PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id_invalid")
    expected = f"https://chatgpt.com/g/g-p-{project_id}-{slug}/project"
    if url.rstrip("/") != expected:
        raise ValueError("project_url_mismatch")
    return {"id": project_id, "slug": slug, "name": name, "url": expected}


def _compile_completeness(
    value: Any,
    chats: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("snapshot_completeness_missing")
    if value.get("status") != "complete":
        raise ValueError("snapshot_not_complete")
    if value.get("owner_reviewed") is not True:
        raise ValueError("snapshot_owner_review_missing")
    expected = {
        "chat_count": len(chats),
        "message_count": sum(len(chat["messages"]) for chat in chats),
        "source_count": len(sources),
    }
    observed = {key: value.get(key) for key in expected}
    if observed != expected:
        raise ValueError("snapshot_completeness_count_mismatch")
    return {"status": "complete", "owner_reviewed": True, **expected}


def _compile_chats(value: Any, project: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("chats_invalid")
    chats: list[dict[str, Any]] = []
    seen_chat_ids: set[str] = set()
    seen_message_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("chat_invalid")
        chat_id = _required_string(raw, "id")
        if not _CHAT_ID.fullmatch(chat_id):
            raise ValueError("chat_id_invalid")
        if chat_id in seen_chat_ids:
            raise ValueError("duplicate_chat_id")
        seen_chat_ids.add(chat_id)
        expected_url = (
            f"https://chatgpt.com/g/g-p-{project['id']}-{project['slug']}/c/{chat_id}"
        )
        url = _required_string(raw, "url")
        if url.rstrip("/") != expected_url:
            raise ValueError("project_chat_url_mismatch")
        messages_raw = raw.get("messages")
        if not isinstance(messages_raw, list) or not messages_raw:
            raise ValueError("messages_invalid")
        messages: list[dict[str, Any]] = []
        for message_raw in messages_raw:
            message = _compile_message(message_raw, seen_message_ids)
            messages.append(message)
        chat: dict[str, Any] = {
            "id": chat_id,
            "title": _required_string(raw, "title"),
            "url": expected_url,
            "captured_at": _timestamp(raw.get("captured_at"), "chat_captured_at_invalid"),
            "messages": messages,
        }
        chat["record_sha256"] = sha256_value(chat)
        chats.append(chat)
    chats.sort(key=lambda item: item["id"])
    return chats


def _compile_message(value: Any, seen_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("message_invalid")
    message_id = _required_string(value, "id")
    if message_id in seen_ids:
        raise ValueError("duplicate_message_id")
    seen_ids.add(message_id)
    role = _required_string(value, "role")
    if role not in {"user", "assistant"}:
        raise ValueError("message_role_invalid")
    text = _normalize_text(_required_string(value, "text"))
    if not text:
        raise ValueError("message_text_empty")
    message: dict[str, Any] = {
        "id": message_id,
        "role": role,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    created_at = value.get("created_at")
    if created_at is not None:
        message["created_at"] = _timestamp(created_at, "message_created_at_invalid")
    message["record_sha256"] = sha256_value(message)
    return message


def _compile_sources(value: Any, project: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("sources_invalid")
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("source_invalid")
        source_id = _required_string(raw, "id")
        if source_id in seen:
            raise ValueError("duplicate_source_id")
        seen.add(source_id)
        text = _normalize_text(_required_string(raw, "text"))
        url = _required_string(raw, "url")
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "file"}:
            raise ValueError("source_url_invalid")
        source: dict[str, Any] = {
            "id": source_id,
            "title": _required_string(raw, "title"),
            "url": url,
            "project_id": project["id"],
            "captured_at": _timestamp(
                raw.get("captured_at"),
                "source_captured_at_invalid",
            ),
            "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        source["record_sha256"] = sha256_value(source)
        sources.append(source)
    sources.sort(key=lambda item: item["id"])
    return sources


def _segments(
    chats: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for chat in chats:
        for message in chat["messages"]:
            segments.extend(
                _segment_record(
                    kind="chat_message",
                    parent_id=f"{chat['id']}:{message['id']}",
                    title=chat["title"],
                    source_url=chat["url"],
                    source_captured_at=chat["captured_at"],
                    source_record_sha256=message["record_sha256"],
                    text=message["text"],
                    role=message["role"],
                )
            )
    for source in sources:
        segments.extend(
            _segment_record(
                kind="project_source",
                parent_id=source["id"],
                title=source["title"],
                source_url=source["url"],
                source_captured_at=source["captured_at"],
                source_record_sha256=source["record_sha256"],
                text=source["text"],
                role=None,
            )
        )
    segments.sort(key=lambda item: item["segment_id"])
    return segments


def _segment_record(
    *,
    kind: str,
    parent_id: str,
    title: str,
    source_url: str,
    source_captured_at: str,
    source_record_sha256: str,
    text: str,
    role: str | None,
) -> list[dict[str, Any]]:
    chunks = _text_chunks(text, SEGMENT_CHARS)
    result: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        identity = {"kind": kind, "parent_id": parent_id, "index": index}
        result.append(
            {
                "segment_id": sha256_value(identity),
                "kind": kind,
                "parent_id": parent_id,
                "index": index,
                "role": role,
                "title": title,
                "text": chunk,
                "text_sha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                "source_url": source_url,
                "source_captured_at": source_captured_at,
                "source_record_sha256": source_record_sha256,
                "prompt_injection_suspected": any(
                    pattern.search(chunk) for pattern in _INJECTION_PATTERNS
                ),
                "authority": CONTEXT_AUTHORITY,
            }
        )
    return result


def _text_chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    return chunks


def _corpus_integrity_reason(
    corpus: dict[str, Any],
    *,
    now: datetime | None,
) -> str | None:
    if corpus.get("schema") != CORPUS_SCHEMA:
        return "corpus_schema_invalid"
    if corpus.get("authority") != CONTEXT_AUTHORITY:
        return "corpus_authority_invalid"
    if corpus.get("source_authenticity") != "owner_supplied_not_independently_verified":
        return "source_authenticity_invalid"
    completeness = corpus.get("completeness")
    if not isinstance(completeness, dict) or completeness.get("status") != "complete":
        return "corpus_completeness_invalid"
    if completeness.get("owner_reviewed") is not True:
        return "corpus_completeness_invalid"
    digest = corpus.get("corpus_sha256")
    if not isinstance(digest, str):
        return "corpus_digest_missing"
    body = {key: value for key, value in corpus.items() if key != "corpus_sha256"}
    if sha256_value(body) != digest:
        return "corpus_digest_mismatch"
    try:
        captured_at = _parse_timestamp(corpus.get("captured_at"))
        max_age_value = corpus.get("max_age_days")
        if not isinstance(max_age_value, int):
            return "corpus_freshness_invalid"
        max_age_days = max_age_value
    except (TypeError, ValueError):
        return "corpus_freshness_invalid"
    if not 1 <= max_age_days <= 3650:
        return "corpus_freshness_invalid"
    if _utc(now) > captured_at + timedelta(days=max_age_days):
        return "corpus_stale"
    chats = corpus.get("chats")
    sources = corpus.get("sources")
    segments = corpus.get("segments")
    counts = corpus.get("counts")
    if not isinstance(chats, list):
        return "corpus_records_invalid"
    if not isinstance(sources, list):
        return "corpus_records_invalid"
    if not isinstance(segments, list):
        return "corpus_records_invalid"
    if not isinstance(counts, dict):
        return "corpus_counts_invalid"
    actual = {
        "chats": len(chats),
        "messages": sum(
            len(chat.get("messages", [])) for chat in chats if isinstance(chat, dict)
        ),
        "sources": len(sources),
        "segments": len(segments),
    }
    if counts != actual:
        return "corpus_counts_mismatch"
    expected_complete_counts = {
        "chat_count": actual["chats"],
        "message_count": actual["messages"],
        "source_count": actual["sources"],
    }
    if {key: completeness.get(key) for key in expected_complete_counts} != (
        expected_complete_counts
    ):
        return "corpus_completeness_count_mismatch"
    if _record_integrity_reason(chats, sources, segments) is not None:
        return "corpus_record_digest_mismatch"
    return None


def _record_integrity_reason(
    chats: list[Any],
    sources: list[Any],
    segments: list[Any],
) -> str | None:
    for chat in chats:
        if not isinstance(chat, dict):
            return "chat_invalid"
        messages = chat.get("messages")
        if not isinstance(messages, list):
            return "messages_invalid"
        for message in messages:
            if not isinstance(message, dict) or not _record_digest_matches(message):
                return "message_digest_mismatch"
        if not _record_digest_matches(chat):
            return "chat_digest_mismatch"
    for source in sources:
        if not isinstance(source, dict) or not _record_digest_matches(source):
            return "source_digest_mismatch"
    expected_segments = _segments(chats, sources)
    if segments != expected_segments:
        return "segment_projection_mismatch"
    return None


def _record_digest_matches(record: dict[str, Any]) -> bool:
    digest = record.get("record_sha256")
    if not isinstance(digest, str):
        return False
    content = {key: value for key, value in record.items() if key != "record_sha256"}
    return sha256_value(content) == digest


def _packet(
    *,
    status: str,
    reason: str | None,
    corpus: dict[str, Any],
    task_sha256: str,
    items: list[dict[str, Any]],
    candidate_count: int,
    selected_chars: int,
    rejection_reasons: dict[str, int],
    max_items: int,
    max_chars: int,
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "schema": PACKET_SCHEMA,
        "status": status,
        "reason": reason,
        "authority": CONTEXT_AUTHORITY,
        "task_sha256": task_sha256,
        "corpus_sha256": corpus.get("corpus_sha256"),
        "project": corpus.get("project"),
        "source_authenticity": corpus.get("source_authenticity"),
        "completeness": corpus.get("completeness"),
        "budget": {"max_items": max_items, "max_chars": max_chars},
        "candidate_count": candidate_count,
        "selected_count": len(items),
        "selected_chars": selected_chars,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "items": items,
    }
    packet["packet_sha256"] = sha256_value(packet)
    return packet


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.strip().split("\n"))


def _required_string(value: dict[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"{key}_invalid")
    return candidate.strip()


def _timestamp(value: Any, reason: str) -> str:
    try:
        parsed = _parse_timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(reason) from exc
    return parsed.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now_timezone_missing")
    return current.astimezone(timezone.utc)


def _count(target: dict[str, int], reason: str) -> None:
    target[reason] = target.get(reason, 0) + 1
