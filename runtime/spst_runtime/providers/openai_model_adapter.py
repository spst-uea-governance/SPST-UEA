import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from spst_runtime.interfaces.model_adapter import ModelAdapter


class OpenAIModelAdapterError(RuntimeError):
    """Raised when an OpenAI model request fails."""


@dataclass
class OpenAIModelAdapter(ModelAdapter):
    """OpenAI Responses API adapter kept behind the SPST model boundary."""

    model: str = "gpt-4.1"
    api_key_env: str = "OPENAI_API_KEY"
    endpoint: str = "https://api.openai.com/v1/responses"
    timeout_seconds: int = 60

    @property
    def available(self) -> bool:
        return bool(os.getenv(self.api_key_env))

    async def infer(self, prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            return {
                "provider": "openai",
                "model": self.model,
                "available": False,
                "text": "",
                "error": f"{self.api_key_env} is not set.",
            }

        payload: dict[str, Any] = {
            "model": os.getenv("SPST_OPENAI_MODEL", self.model),
            "input": prompt,
            "metadata": {
                "runtime": "spst-uea",
            },
        }
        if context:
            payload["instructions"] = context.get("instructions")

        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OpenAIModelAdapterError(f"OpenAI request failed: {exc.code} {detail}") from exc

        return {
            "provider": "openai",
            "model": data.get("model", payload["model"]),
            "available": True,
            "response_id": data.get("id"),
            "status": data.get("status"),
            "text": self._extract_text(data),
        }

    def _extract_text(self, data: dict[str, Any]) -> str:
        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
        return "\n".join(chunk for chunk in chunks if chunk)

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.available,
            "provider": "openai",
            "model": os.getenv("SPST_OPENAI_MODEL", self.model),
            "available": self.available,
            "requires_api_key": True,
        }

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "interface": "ModelAdapter",
            "methods": ["infer", "health", "get_capabilities"],
            "required_provider": "openai",
            "supports_streaming": False,
            "requires_api_key": True,
        }
