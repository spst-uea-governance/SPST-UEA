try:
    from pydantic import BaseModel, Field
except Exception:
    from dataclasses import dataclass as BaseModel
    def Field(default=None, default_factory=None): return default

if hasattr(BaseModel,"model_validate"):
    class SubjectSchema(BaseModel):
        identity: dict = Field(default_factory=dict)
        goals: list = Field(default_factory=list)
        memory_refs: list[str] = Field(default_factory=list)
        metadata: dict = Field(default_factory=dict)
else:
    from dataclasses import dataclass, field
    @dataclass
    class SubjectSchema:
        identity: dict = field(default_factory=dict)
        goals: list = field(default_factory=list)
        memory_refs: list = field(default_factory=list)
        metadata: dict = field(default_factory=dict)
