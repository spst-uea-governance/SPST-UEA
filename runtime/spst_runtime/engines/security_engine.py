import ast
import re
from typing import Any


class SecurityEngine:
    """Deterministic local red-team scanner for secrets and destructive operations."""

    _secret_pattern = re.compile(
        r"(?i)(?:openai_api_key|api[_-]?key|secret|token)\s*=\s*['\"][^'\"]+['\"]"
    )
    _destructive_tokens = (
        "rm -rf",
        "git reset --hard",
        "remove-item -recurse",
        "shutil.rmtree",
        "os.remove",
        "drop table",
    )

    def scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code") or "")
        command = self._command_text(payload.get("command") or payload.get("mcp_request") or "")
        content = f"{code}\n{command}".lower()
        findings = []
        if self._secret_pattern.search(code):
            findings.append({"kind": "secret_exposure", "severity": "critical"})
        for token in self._destructive_tokens:
            if token in content:
                findings.append({"kind": "destructive_operation", "severity": "critical", "token": token})
        if code:
            try:
                tree = ast.parse(code)
            except SyntaxError:
                findings.append({"kind": "invalid_python", "severity": "medium"})
            else:
                if any(isinstance(node, (ast.Delete, ast.Global, ast.Nonlocal)) for node in ast.walk(tree)):
                    findings.append({"kind": "unsafe_ast_mutation", "severity": "high"})
        blocked = any(item["severity"] == "critical" for item in findings)
        return {
            "scanner": "SecuritySubject",
            "findings": findings,
            "severity": "critical" if blocked else "none",
            "blocked": blocked,
        }

    @staticmethod
    def _command_text(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(str(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value)
        return str(value)
