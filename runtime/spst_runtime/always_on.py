from dataclasses import dataclass


@dataclass(frozen=True)
class AlwaysOnConfig:
    mode: str = "codex-chat-mediated"
    provider: str = "codex-mediated-local"
    requires_api_key: bool = False
    default_event: str = "chat_turn"
    default_steps: int = 3
    target_esi: float = 0.95


CONFIG = AlwaysOnConfig()
