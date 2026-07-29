from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

from spst_runtime.project_context import compile_snapshot, dump_json, load_json
from spst_runtime.project_context_supervisor import ProjectContextSupervisor
from spst_runtime.web import CockpitRuntime, handle_cockpit_request


PROJECT_ID = "6a5667f768b881919467d022d9daf511"
CHAT_ID = "6a676582-76ac-83e8-9d99-796fa03645ac"


def _snapshot(*, captured_at: str = "2026-07-29T12:00:00Z") -> dict[str, object]:
    base = f"https://chatgpt.com/g/g-p-{PROJECT_ID}-spst-uea"
    return {
        "schema": "chatgpt-project-export-v1",
        "captured_at": captured_at,
        "project": {
            "id": PROJECT_ID,
            "slug": "spst-uea",
            "name": "SPST-UEA",
            "url": f"{base}/project",
        },
        "completeness": {
            "status": "complete",
            "owner_reviewed": True,
            "chat_count": 1,
            "message_count": 1,
            "source_count": 0,
        },
        "chats": [
            {
                "id": CHAT_ID,
                "title": "Supervisor context",
                "url": f"{base}/c/{CHAT_ID}",
                "captured_at": captured_at,
                "messages": [
                    {
                        "id": "message-1",
                        "role": "user",
                        "text": "Repository evidence supervisor latency contract.",
                        "created_at": captured_at,
                    }
                ],
            }
        ],
        "sources": [],
    }


def _corpus(tmp_path: Path, *, captured_at: str = "2026-07-29T12:00:00Z") -> Path:
    path = tmp_path / "corpus.json"
    now = datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc)
    dump_json(path, compile_snapshot(_snapshot(captured_at=captured_at), now=now))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_supervisor_status_is_read_only_and_query_reuses_one_worker(tmp_path: Path):
    corpus = _corpus(tmp_path)
    supervisor = ProjectContextSupervisor(corpus)
    before = _sha256(corpus)

    initial = supervisor.status()
    first = supervisor.query({"query": "repository evidence supervisor"})
    second = supervisor.query({"query": "repository evidence supervisor"})
    running = supervisor.status()
    supervisor.close()

    assert initial["status"] == "stopped"
    assert initial["request_count"] == 0
    assert initial["persistent_state_written"] is False
    assert first["status"] == "ready"
    assert first["response"]["packet_sha256"] == second["response"]["packet_sha256"]
    assert first["generation"] == second["generation"] == 1
    assert running["child_running"] is True
    assert running["request_count"] == 2
    assert _sha256(corpus) == before
    assert supervisor.status()["child_running"] is False


def test_supervisor_fails_closed_on_tamper_and_recovers_after_exact_restore(
    tmp_path: Path,
):
    corpus = _corpus(tmp_path)
    original = corpus.read_bytes()
    supervisor = ProjectContextSupervisor(corpus)
    first = supervisor.query({"query": "repository evidence supervisor"})
    altered = load_json(corpus)
    altered["segments"][0]["text"] = "altered"
    dump_json(corpus, altered)

    blocked = supervisor.query({"query": "repository evidence supervisor"})
    corpus.write_bytes(original)
    restored = supervisor.query({"query": "repository evidence supervisor"})
    supervisor.close()

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "corpus_digest_mismatch"
    assert restored["status"] == "ready"
    assert restored["response"]["packet_sha256"] == first["response"]["packet_sha256"]
    assert restored["generation"] == first["generation"]


def test_supervisor_rejects_stale_corpus(tmp_path: Path):
    corpus = _corpus(tmp_path, captured_at="2020-01-01T00:00:00Z")
    supervisor = ProjectContextSupervisor(corpus)

    blocked = supervisor.query({"query": "repository evidence supervisor"})
    supervisor.close()

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "corpus_stale"


def test_supervisor_restarts_before_next_request_after_child_exit(tmp_path: Path):
    corpus = _corpus(tmp_path)
    supervisor = ProjectContextSupervisor(corpus)
    first = supervisor.query({"query": "repository evidence supervisor"})
    first_pid = supervisor.status()["child_pid"]
    assert isinstance(first_pid, int)
    os.kill(first_pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while supervisor.status()["child_running"] and time.monotonic() < deadline:
        time.sleep(0.01)

    second = supervisor.query({"query": "repository evidence supervisor"})
    status = supervisor.status()
    supervisor.close()

    assert first["generation"] == 1
    assert second["status"] == "ready"
    assert second["generation"] == 2
    assert status["restart_count"] == 1
    assert status["child_pid"] != first_pid


def test_supervisor_never_retries_unresolved_inflight_request(tmp_path: Path):
    corpus = _corpus(tmp_path)
    command = (
        sys.executable,
        "-c",
        "import sys; sys.stdin.readline()",
    )
    supervisor = ProjectContextSupervisor(corpus, child_command=command)

    first = supervisor.query({"query": "repository evidence supervisor"})
    second = supervisor.query({"query": "repository evidence supervisor"})
    status = supervisor.status()
    supervisor.close()

    assert first["status"] == "blocked"
    assert second["status"] == "blocked"
    assert first["reason"] == "project_context_request_outcome_unresolved"
    assert second["reason"] == "project_context_request_outcome_unresolved"
    assert status["request_count"] == 2
    assert status["generation"] == 2
    assert status["restart_count"] == 1
    assert status["automatic_inflight_retry"] is False


def test_supervisor_rejects_forged_ready_worker_response(tmp_path: Path):
    corpus = _corpus(tmp_path)
    command = (
        sys.executable,
        "-c",
        (
            "import sys; "
            "sys.stdin.readline(); "
            "print('{\"schema\":\"spst-chatgpt-project-context-packet-v1\",'"
            "+'\"status\":\"ready\",\"packet_sha256\":\"'+'0'*64+'\",'"
            "+'\"corpus_sha256\":\"'+'0'*64+'\"}', flush=True)"
        ),
    )
    supervisor = ProjectContextSupervisor(corpus, child_command=command)

    result = supervisor.query({"query": "repository evidence supervisor"})
    supervisor.close()

    assert result["status"] == "blocked"
    assert result["reason"] == "project_context_worker_response_invalid"


def test_supervisor_rejects_non_finite_worker_json(tmp_path: Path):
    corpus = _corpus(tmp_path)
    command = (
        sys.executable,
        "-c",
        (
            "import sys; sys.stdin.readline(); "
            "print('{\"schema\":\"spst-chatgpt-project-context-packet-v1\",'"
            "+'\"status\":\"ready\",\"packet_sha256\":NaN}', flush=True)"
        ),
    )
    supervisor = ProjectContextSupervisor(corpus, child_command=command)

    result = supervisor.query({"query": "repository evidence supervisor"})
    supervisor.close()

    assert result["status"] == "blocked"
    assert result["reason"] == "project_context_worker_response_invalid"


def test_supervisor_serializes_concurrent_queries(tmp_path: Path):
    corpus = _corpus(tmp_path)
    supervisor = ProjectContextSupervisor(corpus)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: supervisor.query(
                    {"query": "repository evidence supervisor"}
                ),
                range(8),
            )
        )
    status = supervisor.status()
    supervisor.close()

    assert {result["status"] for result in results} == {"ready"}
    assert {result["generation"] for result in results} == {1}
    assert len({result["response"]["packet_sha256"] for result in results}) == 1
    assert status["request_count"] == 8
    assert status["failure_count"] == 0


def test_cockpit_exposes_official_supervisor_status_and_query_paths(tmp_path: Path):
    corpus = _corpus(tmp_path)
    supervisor = ProjectContextSupervisor(corpus)
    cockpit = CockpitRuntime(
        db_path=str(tmp_path / "cockpit.db"),
        project_context_supervisor=supervisor,
    )

    status_code, initial = handle_cockpit_request(
        "GET",
        "/api/project-context/status",
        runtime=cockpit,
    )
    query_code, result = handle_cockpit_request(
        "POST",
        "/api/project-context/query",
        body=json.dumps({"query": "repository evidence supervisor"}).encode(),
        runtime=cockpit,
    )
    aggregate_code, aggregate = handle_cockpit_request(
        "GET",
        "/api/status",
        runtime=cockpit,
    )
    cockpit.close()

    assert status_code == query_code == aggregate_code == HTTPStatus.OK
    assert initial["child_running"] is False
    assert result["status"] == "ready"
    assert aggregate["project_context_supervisor"]["child_running"] is True
