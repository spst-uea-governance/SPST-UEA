from dataclasses import dataclass, field
from typing import Any

@dataclass
class SubjectState:
    identity: dict[str, Any]=field(default_factory=dict)
    goals: list[dict[str, Any]]=field(default_factory=list)
    memory_refs: list[str]=field(default_factory=list)
    governance: dict[str, Any]=field(default_factory=dict)
    metadata: dict[str, Any]=field(default_factory=dict)
