from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

@dataclass
class Checkpoint:
    version: int
    timestamp: datetime
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    reconstruction_metadata: dict[str, Any] = field(default_factory=dict)
