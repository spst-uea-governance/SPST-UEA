from typing import Any

from pydantic import BaseModel, Field


class SubjectSchema(BaseModel):
    identity: dict[str, Any] = Field(default_factory=dict)
    goals: list[dict[str, Any]] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
