from dataclasses import dataclass
from typing import Any

from spst_runtime.interfaces.model_adapter import ModelAdapter


@dataclass
class LocalCodexAdapter(ModelAdapter):
    """API-key-free adapter for Codex-mediated local runtime operation."""

    name: str = "codex-mediated-local"

    async def infer(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "text": (
                "Prompt accepted inside the SPST-UEA model boundary. "
                "This no-key mode keeps inference local/Codex-mediated rather than calling OpenAI API."
            ),
            "prompt": prompt,
            "context": context or {},
            "requires_api_key": False,
        }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.name,
            "available": True,
            "requires_api_key": False,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "methods": ["infer", "health", "get_capabilities"],
            "required_provider": "none",
            "supports_streaming": False,
            "requires_api_key": False,
        }
