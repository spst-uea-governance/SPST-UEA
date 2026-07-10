import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spst_runtime.interfaces.tool_adapter import ToolAdapter


@dataclass
class ToolProvider(ToolAdapter):
    """Deterministic local sandbox for diagnosis and self-repair planning."""

    sandbox_name: str = "local-self-repair"

    def __post_init__(self) -> None:
        self.dynamic_dir = Path(__file__).resolve().parents[1] / "tools" / "dynamic"
        self.dynamic_dir.mkdir(parents=True, exist_ok=True)
        self._dynamic_tools: dict[str, Path] = {}

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
        if tool in self._dynamic_tools:
            return self.run_dynamic_tool(tool, payload)
        return {
            "tool": tool,
            "status": "noop",
            "sandbox": self.sandbox_name,
        }

    def generate_dynamic_tool(self, task: str, prompt: str) -> dict[str, Any]:
        safe_name = self._safe_tool_name(task)
        tool_path = self.dynamic_dir / f"{safe_name}.py"
        source = self._render_dynamic_tool_source(safe_name)
        tool_path.write_text(source, encoding="utf-8")
        validation = self._validate_dynamic_tool_source(source)
        if validation["ok"]:
            self._dynamic_tools[safe_name] = tool_path
        return {
            "tool": "generate_dynamic_tool",
            "tool_name": safe_name,
            "status": "mounted" if validation["ok"] else "rejected",
            "path": str(tool_path),
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
        return sorted(["diagnose", "repair", "generate_dynamic_tool", *self._dynamic_tools])

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.sandbox_name,
            "external_network": False,
            "dynamic_tools": self.list_tools(),
        }

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
