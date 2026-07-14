import hashlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spst_runtime.interfaces.tool_adapter import ToolAdapter
from spst_runtime.verification_profiles import command_for, timeout_for


@dataclass
class ToolProvider(ToolAdapter):
    """Deterministic local sandbox for diagnosis and self-repair planning."""

    sandbox_name: str = "local-self-repair"
    workspace_root: str | None = None

    def __post_init__(self) -> None:
        self._dynamic_tools: dict[str, str] = {}
        self._workspace_root = Path(self.workspace_root or Path.cwd()).resolve()

    def run(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool == "diagnose":
            return {
                "tool": tool,
                "status": "diagnosed",
                "issue": payload.get("issue", "unknown"),
                "sandbox": self.sandbox_name,
            }
        if tool == "repair":
            return {
                "tool": tool,
                "status": "repaired",
                "patch": {
                    "phase2_reflection": {"esi": 0.95, "status": "accepted"},
                    "self_repair_applied": True,
                },
                "sandbox": self.sandbox_name,
            }
        if tool == "generate_dynamic_tool":
            return self.generate_dynamic_tool(
                str(payload.get("task") or "frontier_verifier"),
                str(payload.get("prompt") or ""),
            )
        if tool == "mcp":
            return self.handle_mcp(payload.get("request", {}), governance_authorized=bool(payload.get("authorized")))
        if tool == "execute_local":
            return self.execute_local(payload.get("command", []), governance_authorized=bool(payload.get("authorized")))
        if tool == "verify_local":
            return self.execute_verification_profile(
                str(payload.get("profile") or ""),
                governance_authorized=bool(payload.get("authorized")),
            )
        if tool in self._dynamic_tools:
            return self.run_dynamic_tool(tool, payload)
        return {
            "tool": tool,
            "status": "noop",
            "sandbox": self.sandbox_name,
        }

    def generate_dynamic_tool(self, task: str, prompt: str) -> dict[str, Any]:
        safe_name = self._safe_tool_name(task)
        source = self._render_dynamic_tool_source(safe_name)
        validation = self._validate_dynamic_tool_source(source)
        if validation["ok"]:
            self._dynamic_tools[safe_name] = source
        return {
            "tool": "generate_dynamic_tool",
            "tool_name": safe_name,
            "status": "mounted" if validation["ok"] else "rejected",
            "path": None,
            "artifact": {
                "storage": "in_memory",
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
            "validation": validation,
            "prompt_fingerprint": self._fingerprint(prompt),
            "sandbox": self.sandbox_name,
        }

    def run_dynamic_tool(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool not in self._dynamic_tools:
            return {"tool": tool, "status": "missing", "sandbox": self.sandbox_name}
        prompt = str(payload.get("prompt") or "")
        return {
            "tool": tool,
            "status": "solved",
            "result": {
                "rule": "Use a deterministic verifier before committing unknown frontier actions.",
                "matched_frontier_terms": [
                    term for term in ("unknown", "frontier", "deterministic", "verifier")
                    if term in prompt.lower()
                ],
            },
            "sandbox": self.sandbox_name,
        }

    def list_tools(self) -> list[str]:
        return sorted(
            [
                "diagnose",
                "repair",
                "generate_dynamic_tool",
                "local.execute",
                "verify.local",
                *self._dynamic_tools,
            ]
        )

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.sandbox_name,
            "external_network": False,
            "dynamic_tools": self.list_tools(),
            "mcp": {"jsonrpc": "2.0", "local_only": True},
        }

    def handle_mcp(self, request: dict[str, Any], *, governance_authorized: bool) -> dict[str, Any]:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return self._mcp_error(request_id, -32600, "invalid_jsonrpc")
        method = request.get("method")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}},
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [{"name": "local.execute", "description": "Governed local command profiles"}]},
            }
        if method != "tools/call":
            return self._mcp_error(request_id, -32601, "method_not_found")
        params = request.get("params", {}) or {}
        if params.get("name") != "local.execute":
            return self._mcp_error(request_id, -32602, "unknown_tool")
        arguments = params.get("arguments", {}) or {}
        result = self.execute_local(arguments.get("command", []), governance_authorized=governance_authorized)
        if result["status"] == "denied":
            return self._mcp_error(request_id, -32001, result["reason"])
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def execute_local(self, command: Any, *, governance_authorized: bool) -> dict[str, Any]:
        if not governance_authorized:
            return {"status": "denied", "reason": "governance_required", "sandbox": self.sandbox_name}
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            return {"status": "denied", "reason": "invalid_command", "sandbox": self.sandbox_name}
        permitted_profiles = {
            ("python", "--version"),
            ("git", "status", "--short"),
            ("git", "diff", "--check"),
            ("pytest", "--version"),
            ("mypy", "--version"),
            ("ruff", "--version"),
        }
        if tuple(command) not in permitted_profiles:
            return {"status": "denied", "reason": "command_profile_not_permitted", "sandbox": self.sandbox_name}
        completed = subprocess.run(
            command,
            cwd=self._workspace_root,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            check=False,
        )
        return {
            "status": "completed" if completed.returncode == 0 else "failed",
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-2000:],
            "stderr": completed.stderr[-2000:],
            "sandbox": self.sandbox_name,
        }

    def execute_verification_profile(
        self,
        profile: str,
        *,
        governance_authorized: bool,
    ) -> dict[str, Any]:
        """Run a fixed local profile without accepting arbitrary command input."""
        if not governance_authorized:
            return {
                "profile": profile,
                "status": "denied",
                "reason": "governance_required",
                "sandbox": self.sandbox_name,
            }
        command = command_for(profile)
        if command is None:
            return {
                "profile": profile,
                "status": "denied",
                "reason": "verification_profile_not_permitted",
                "sandbox": self.sandbox_name,
            }

        started_at = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=self._workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout_for(profile),
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            return self._verification_result(
                profile,
                "unavailable",
                None,
                "",
                "executable_not_found",
                started_at,
            )
        except subprocess.TimeoutExpired as error:
            return self._verification_result(
                profile,
                "timed_out",
                None,
                self._text(error.stdout),
                self._text(error.stderr),
                started_at,
            )
        return self._verification_result(
            profile,
            "completed" if completed.returncode == 0 else "failed",
            completed.returncode,
            completed.stdout,
            completed.stderr,
            started_at,
            command=list(command),
        )

    def _verification_result(
        self,
        profile: str,
        status: str,
        returncode: int | None,
        stdout: str,
        stderr: str,
        started_at: float,
        *,
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        output = f"{stdout}\n{stderr}"
        result: dict[str, Any] = {
            "profile": profile,
            "status": status,
            "returncode": returncode,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
            "output_digest": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "stdout": stdout[-2000:],
            "stderr": stderr[-2000:],
            "sandbox": self.sandbox_name,
        }
        if command is not None:
            result["command"] = command
        return result

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    @staticmethod
    def _mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _safe_tool_name(self, task: str) -> str:
        normalized = "".join(char if char.isalnum() else "_" for char in task.lower()).strip("_")
        return normalized or "dynamic_tool"

    def _render_dynamic_tool_source(self, tool_name: str) -> str:
        return (
            '"""Deterministic SPST-UEA dynamic tool generated inside the local sandbox."""\n\n'
            "def run(payload):\n"
            "    prompt = str(payload.get('prompt') or '')\n"
            "    terms = ['unknown', 'frontier', 'deterministic', 'verifier']\n"
            "    return {\n"
            f"        'tool': '{tool_name}',\n"
            "        'status': 'solved',\n"
            "        'matched_frontier_terms': [term for term in terms if term in prompt.lower()],\n"
            "        'rule': 'Use a deterministic verifier before committing unknown frontier actions.',\n"
            "    }\n"
        )

    def _validate_dynamic_tool_source(self, source: str) -> dict[str, Any]:
        forbidden = ("import os", "import subprocess", "socket", "requests", "open(")
        blocked = [token for token in forbidden if token in source]
        namespace: dict[str, Any] = {}
        if blocked:
            return {"ok": False, "blocked_tokens": blocked}
        exec(source, {"__builtins__": {"str": str}}, namespace)
        result = namespace["run"]({"prompt": "unknown deterministic verifier"})
        return {
            "ok": result.get("status") == "solved",
            "blocked_tokens": [],
            "test_result": result,
        }

    def _fingerprint(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
