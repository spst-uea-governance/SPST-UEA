"""Persistent owner for the read-only Project Context JSONL worker."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import Any, Sequence


SUPERVISOR_RESPONSE_SCHEMA = "spst-project-context-supervisor-response-v1"
SUPERVISOR_STATUS_SCHEMA = "spst-project-context-supervisor-status-v1"
PROJECT_CONTEXT_PACKET_SCHEMA = "spst-chatgpt-project-context-packet-v1"
PROJECT_CONTEXT_BLOCKED_SCHEMA = "spst-chatgpt-project-context-cli-result-v1"


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class ProjectContextSupervisor:
    """Serialize requests through one restartable, byte-validating JSONL child."""

    def __init__(
        self,
        corpus_path: str | Path,
        *,
        request_timeout_seconds: float = 15.0,
        child_command: Sequence[str] | None = None,
    ) -> None:
        if not 0.1 <= request_timeout_seconds <= 60.0:
            raise ValueError("project_context_supervisor_timeout_invalid")
        self.corpus_path = Path(corpus_path).expanduser().resolve()
        self.request_timeout_seconds = request_timeout_seconds
        self._child_command = tuple(child_command) if child_command is not None else None
        self._lock = Lock()
        self._child: subprocess.Popen[str] | None = None
        self._generation = 0
        self._restart_count = 0
        self._request_count = 0
        self._failure_count = 0
        self._last_exit_code: int | None = None
        self._last_status: str | None = None
        self._last_reason: str | None = None
        self._last_packet_sha256: str | None = None
        self._last_corpus_sha256: str | None = None

    def query(self, request: dict[str, Any]) -> dict[str, Any]:
        """Forward one request exactly once; never retry an in-flight request."""

        with self._lock:
            if not isinstance(request, dict) or not isinstance(request.get("query"), str):
                return self._blocked("query_request_invalid")
            try:
                child = self._ensure_child()
            except OSError:
                return self._blocked("project_context_worker_start_failed")
            self._request_count += 1
            serialized = json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            try:
                if child.stdin is None:
                    raise BrokenPipeError
                child.stdin.write(serialized + "\n")
                child.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._discard_child()
                return self._blocked("project_context_request_outcome_unresolved")

            line = self._readline(child)
            if line is None:
                self._discard_child()
                return self._blocked("project_context_request_outcome_unresolved")
            try:
                response = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non_finite_json:{value}")
                    ),
                )
            except (json.JSONDecodeError, ValueError):
                self._discard_child()
                return self._blocked("project_context_worker_response_invalid")
            if not _valid_worker_response(response):
                self._discard_child()
                return self._blocked("project_context_worker_response_invalid")
            status = response["status"]
            self._last_status = str(status) if status is not None else None
            self._last_reason = (
                str(response.get("reason")) if response.get("reason") is not None else None
            )
            self._last_packet_sha256 = (
                str(response.get("packet_sha256"))
                if response.get("packet_sha256") is not None
                else None
            )
            self._last_corpus_sha256 = (
                str(response.get("corpus_sha256"))
                if response.get("corpus_sha256") is not None
                else None
            )
            return {
                "schema": SUPERVISOR_RESPONSE_SCHEMA,
                "status": status,
                "reason": response.get("reason"),
                "generation": self._generation,
                "request_count": self._request_count,
                "automatic_inflight_retry": False,
                "response_sha256": _canonical_hash(response),
                "response": response,
            }

    def status(self) -> dict[str, Any]:
        """Return process and last-observation state without starting or writing."""

        with self._lock:
            child = self._child
            exit_code = child.poll() if child is not None else self._last_exit_code
            running = child is not None and exit_code is None
            corpus_exists = self.corpus_path.is_file()
            corpus_size = self.corpus_path.stat().st_size if corpus_exists else None
            return {
                "schema": SUPERVISOR_STATUS_SCHEMA,
                "status": "running" if running else "stopped",
                "configured": True,
                "corpus_exists": corpus_exists,
                "corpus_size": corpus_size,
                "child_running": running,
                "child_pid": child.pid if running and child is not None else None,
                "child_exit_code": exit_code,
                "generation": self._generation,
                "restart_count": self._restart_count,
                "request_count": self._request_count,
                "failure_count": self._failure_count,
                "last_response_status": self._last_status,
                "last_reason": self._last_reason,
                "last_packet_sha256": self._last_packet_sha256,
                "last_corpus_sha256": self._last_corpus_sha256,
                "automatic_inflight_retry": False,
                "persistent_state_written": False,
                "integrity_current": None,
                "integrity_current_reason": "verified_on_query_not_status",
            }

    def close(self) -> None:
        with self._lock:
            self._discard_child(graceful=True)

    def _command(self) -> list[str]:
        if self._child_command is not None:
            return list(self._child_command)
        return [
            sys.executable,
            "-m",
            "spst_runtime.project_context_bridge",
            "serve",
            "--corpus",
            str(self.corpus_path),
        ]

    def _ensure_child(self) -> subprocess.Popen[str]:
        if self._child is not None and self._child.poll() is None:
            return self._child
        restarting = self._generation > 0
        if self._child is not None:
            self._last_exit_code = self._child.poll()
            self._discard_child()
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        self._child = subprocess.Popen(
            self._command(),
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )
        self._generation += 1
        if restarting:
            self._restart_count += 1
        return self._child

    def _readline(self, child: subprocess.Popen[str]) -> str | None:
        stdout = child.stdout
        if stdout is None:
            return None
        result: Queue[str | BaseException] = Queue(maxsize=1)

        def read() -> None:
            try:
                result.put(stdout.readline())
            except BaseException as error:
                result.put(error)

        reader = Thread(target=read, daemon=True)
        reader.start()
        reader.join(self.request_timeout_seconds)
        if reader.is_alive():
            return None
        observed = result.get_nowait()
        if isinstance(observed, BaseException) or not observed:
            return None
        return observed

    def _discard_child(self, *, graceful: bool = False) -> None:
        child = self._child
        self._child = None
        if child is None:
            return
        if graceful and child.stdin is not None:
            try:
                child.stdin.close()
            except OSError:
                pass
        if child.poll() is None:
            if graceful:
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.terminate()
            else:
                child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
        for stream in (child.stdin, child.stdout):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        self._last_exit_code = child.returncode

    def _blocked(self, reason: str) -> dict[str, Any]:
        self._failure_count += 1
        self._last_status = "blocked"
        self._last_reason = reason
        response = {
            "schema": "spst-chatgpt-project-context-cli-result-v1",
            "status": "blocked",
            "reason": reason,
        }
        return {
            "schema": SUPERVISOR_RESPONSE_SCHEMA,
            "status": "blocked",
            "reason": reason,
            "generation": self._generation,
            "request_count": self._request_count,
            "automatic_inflight_retry": False,
            "response_sha256": _canonical_hash(response),
            "response": response,
        }


def _valid_worker_response(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    status = response.get("status")
    schema = response.get("schema")
    if status == "blocked":
        return (
            schema == PROJECT_CONTEXT_BLOCKED_SCHEMA
            and isinstance(response.get("reason"), str)
            and bool(response["reason"])
        )
    if status not in {"ready", "empty"} or schema != PROJECT_CONTEXT_PACKET_SCHEMA:
        return False
    packet_sha256 = response.get("packet_sha256")
    corpus_sha256 = response.get("corpus_sha256")
    if not all(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in (packet_sha256, corpus_sha256)
    ):
        return False
    packet_content = {
        key: value for key, value in response.items() if key != "packet_sha256"
    }
    return _canonical_hash(packet_content) == packet_sha256
