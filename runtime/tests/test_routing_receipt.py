import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from spst_runtime.chat_bridge import get_chat_status, run_chat_turn, verify_chat_receipt
from spst_runtime.chat_session import SESSION_KEY
from spst_runtime.persistence.sqlite_repository import SQLiteRepository
from spst_runtime.routing_receipt import RoutingReceiptLedger, build_routing_receipt


def _file_manifest(path: Path) -> dict[str, tuple[int, str]]:
    return {
        candidate.name: (candidate.stat().st_size, hashlib.sha256(candidate.read_bytes()).hexdigest())
        for candidate in path.parent.glob(f"{path.name}*")
        if candidate.is_file()
    }


def _run_isolated_turn(tmp_path: Path, prompt: str = "route this task") -> tuple[dict, Path]:
    session_path = tmp_path / "chat-state.db"
    result = run_chat_turn(
        prompt,
        steps=1,
        event="receipt_test",
        session_path=str(session_path),
        memory_path=str(tmp_path / "memory.db"),
    )
    return result, session_path


def test_routed_turn_emits_persisted_verifiable_receipt(tmp_path: Path):
    prompt = "sensitive task body is represented by a digest"
    result, session_path = _run_isolated_turn(tmp_path, prompt)
    receipt = result["routing_receipt"]

    assert receipt["schema"] == "spst-routing-receipt-v1"
    assert receipt["payload"]["route"]["prompt_sha256"] == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()
    assert prompt not in json.dumps(receipt, sort_keys=True)
    assert receipt["payload"]["pipeline"]["trace"] == [
        "observe",
        "retrieve",
        "infer",
        "reflect",
        "govern",
        "commit",
        "act",
    ]
    assert receipt["payload"]["governance"]["authorized"] is True
    assert receipt["payload"]["session"]["turn"] == 1
    assert receipt["payload"]["session"]["memory_record_id"]
    assert receipt["verification"]["verified"] is True

    independent = verify_chat_receipt(receipt["receipt_id"], str(session_path))
    assert independent["verified"] is True
    assert independent["receipt_id"] == receipt["receipt_id"]
    assert independent["provenance"]["valid"] is True


def test_status_reads_existing_session_without_changing_any_source_file(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    before = _file_manifest(session_path)

    status = get_chat_status(str(session_path))

    after = _file_manifest(session_path)
    assert after == before
    assert status["persistence"]["access"] == "read_only_immutable"
    assert status["persistence"]["source_unchanged"] is True
    assert status["persistence"]["state"] == "present"
    assert status["persistence"]["provenance"]["valid"] is True
    assert status["routing"]["verified_receipts"] == 1
    assert status["routing"]["verified_turns"] == 1
    assert status["routing"]["eligible_turns"] == 1
    assert status["routing"]["receipt_coverage"] == 1.0
    assert status["routing"]["latest_receipt_id"] == result["routing_receipt"]["receipt_id"]
    assert status["routing"]["global_codex_task_coverage"] is None
    assert status["routing"]["global_coverage_reason"] == "codex_task_denominator_unavailable"
    assert status["routing"]["task_quality_delta"] is None
    assert status["routing"]["task_quality_reason"] == "paired_outcome_measurement_unavailable"


def test_multiple_receipts_for_one_turn_do_not_inflate_coverage(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    original = result["routing_receipt"]
    alternate = build_routing_receipt(
        prompt="alternate claim for the same persisted turn",
        event="receipt_test",
        steps=1,
        pipeline=result["runtime"]["pipeline"],
        session_turn=1,
        session_state_hash=original["payload"]["session"]["state_record_hash"],
        memory_record_id=original["payload"]["session"]["memory_record_id"],
    )
    RoutingReceiptLedger(str(session_path)).persist(alternate)

    status = get_chat_status(str(session_path))

    assert status["routing"]["verified_receipts"] == 2
    assert status["routing"]["verified_turns"] == 1
    assert status["routing"]["eligible_turns"] == 1
    assert status["routing"]["receipt_coverage"] == 1.0


def test_status_for_missing_database_is_side_effect_free(tmp_path: Path):
    session_path = tmp_path / "missing" / "chat-state.db"

    status = get_chat_status(str(session_path))

    assert status["turn_count"] == 0
    assert status["persistence"]["state"] == "missing"
    assert not session_path.exists()
    assert not Path(f"{session_path}.provenance_key").exists()
    assert list(tmp_path.rglob("*")) == []


def test_missing_or_tampered_receipt_is_not_verified(tmp_path: Path):
    result, session_path = _run_isolated_turn(tmp_path)
    receipt_id = result["routing_receipt"]["receipt_id"]

    missing = verify_chat_receipt("0" * 64, str(session_path))
    assert missing == {
        "verified": False,
        "receipt_id": "0" * 64,
        "reason": "receipt_missing",
    }

    receipt_key = f"routing_receipt:v1:{receipt_id}"
    conn = sqlite3.connect(session_path)
    try:
        serialized = conn.execute(
            "SELECT value FROM state_store WHERE key = ?", (receipt_key,)
        ).fetchone()[0]
        receipt = json.loads(serialized)
        receipt["payload"]["route"]["event"] = "tampered"
        conn.execute(
            "UPDATE state_store SET value = ? WHERE key = ?",
            (json.dumps(receipt, sort_keys=True), receipt_key),
        )
        conn.commit()
    finally:
        conn.close()

    tampered = verify_chat_receipt(receipt_id, str(session_path))
    assert tampered["verified"] is False
    assert tampered["reason"] in {"receipt_digest_mismatch", "state_hash_mismatch"}


def test_incomplete_pipeline_cannot_claim_a_routing_receipt():
    with pytest.raises(ValueError, match="complete SPST trace"):
        build_routing_receipt(
            prompt="not fully routed",
            event="chat_turn",
            steps=1,
            pipeline={"last_trace": ["observe", "infer"], "version": 1},
            session_turn=1,
            session_state_hash="a" * 64,
            memory_record_id="memory-1",
        )


def test_read_only_repository_rejects_writes(tmp_path: Path):
    path = tmp_path / "state.db"
    writer = SQLiteRepository(str(path))
    asyncio.run(writer.save(SESSION_KEY, {"turn_count": 0}))

    reader = SQLiteRepository(str(path), read_only=True)
    with pytest.raises(PermissionError, match="read-only"):
        asyncio.run(reader.save(SESSION_KEY, {"turn_count": 99}))
