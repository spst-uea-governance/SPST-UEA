from dataclasses import dataclass, field
from typing import Any

from spst_runtime.interfaces.model_adapter import ModelAdapter
from spst_runtime.providers.local_codex_adapter import LocalCodexAdapter


@dataclass
class ModelProvider(ModelAdapter):
    """Vendor-neutral model provider with deterministic no-key fallback."""

    primary: ModelAdapter | None = None
    fallback: ModelAdapter = field(default_factory=LocalCodexAdapter)

    async def infer(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        adapter = self._select_adapter()
        result = await adapter.infer(prompt, context or {})
        if result.get("available", True) is False:
            result = await self.fallback.infer(prompt, context or {})
            result["fallback_reason"] = "primary_unavailable"
        return result

    def health(self) -> dict[str, Any]:
        adapter = self._select_adapter()
        adapter_health = adapter.health()
        return {
            "ok": bool(adapter_health.get("ok", False)),
            "active_provider": adapter_health.get("provider"),
            "requires_api_key": bool(adapter_health.get("requires_api_key", False)),
            "fallback_provider": self.fallback.health().get("provider"),
        }

    def get_capabilities(self) -> dict[str, Any]:
        adapter = self._select_adapter()
        capabilities = adapter.get_capabilities()
        return {
            "interface": "ModelAdapter",
            "methods": ["infer", "health", "get_capabilities"],
            "required_provider": capabilities.get("required_provider", "none"),
            "active_provider": adapter.health().get("provider"),
            "fallback_provider": self.fallback.health().get("provider"),
        }

    def _select_adapter(self) -> ModelAdapter:
        if self.primary is None:
            return self.fallback

        health = self.primary.health()
        if health.get("ok", False):
            return self.primary
        return self.fallback
