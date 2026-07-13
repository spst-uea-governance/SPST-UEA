from dataclasses import asdict, dataclass
from typing import Literal


ProfileName = Literal["light", "standard", "strict"]
RequestedProfile = Literal["auto", "light", "standard", "strict"]


_RISK_SIGNALS = (
    "architecture change",
    "breaking change",
    "delete",
    "drop table",
    "production",
    "rewrite history",
    "reset --hard",
    "payment",
    "billing",
    "credential",
    "secret",
    "削除",
    "破壊",
    "本番",
    "履歴書換",
    "支払い",
    "課金",
    "認証情報",
)
_CONTINUITY_SIGNALS = (
    "repository",
    "phase",
    "implement",
    "migration",
    "debug",
    "test",
    "evidence",
    "audit",
    "governance",
    "long-term",
    "multi-step",
    "workspace",
    "実装",
    "検証",
    "テスト",
    "監査",
    "証拠",
    "長期",
    "継続",
    "リポジトリ",
    "課題",
    "解決",
)
_STRICT_EVENTS = {"breaking_change", "architecture_change", "production_change"}


@dataclass(frozen=True)
class ExecutionProfile:
    name: ProfileName
    memory_mode: Literal["session_only", "filtered_long_term"]
    governance_mode: Literal["baseline", "standard", "strict"]
    memory_ttl_seconds: int | None
    minimum_memory_confidence: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def _profile(name: ProfileName, reasons: list[str]) -> ExecutionProfile:
    if name == "light":
        return ExecutionProfile(
            name="light",
            memory_mode="session_only",
            governance_mode="baseline",
            memory_ttl_seconds=None,
            minimum_memory_confidence=1.0,
            reasons=tuple(reasons),
        )
    if name == "strict":
        return ExecutionProfile(
            name="strict",
            memory_mode="filtered_long_term",
            governance_mode="strict",
            memory_ttl_seconds=90 * 24 * 60 * 60,
            minimum_memory_confidence=0.7,
            reasons=tuple(reasons),
        )
    return ExecutionProfile(
        name="standard",
        memory_mode="filtered_long_term",
        governance_mode="standard",
        memory_ttl_seconds=30 * 24 * 60 * 60,
        minimum_memory_confidence=0.5,
        reasons=tuple(reasons),
    )


def select_execution_profile(
    prompt: str,
    *,
    event: str = "chat_turn",
    requested: RequestedProfile = "auto",
) -> ExecutionProfile:
    """Choose the least costly profile that satisfies deterministic safety floors."""
    if requested not in {"auto", "light", "standard", "strict"}:
        raise ValueError(f"Unknown execution profile: {requested}")

    normalized = " ".join(prompt.lower().split())
    risk_signals = [signal for signal in _RISK_SIGNALS if signal in normalized]
    continuity_signals = [signal for signal in _CONTINUITY_SIGNALS if signal in normalized]
    if event in _STRICT_EVENTS or risk_signals:
        reasons = ["risk_floor"]
        reasons.extend(f"risk_signal:{signal}" for signal in risk_signals[:3])
        if event in _STRICT_EVENTS:
            reasons.append(f"strict_event:{event}")
        return _profile("strict", reasons)

    if requested == "strict":
        return _profile("strict", ["explicit_profile"])
    if continuity_signals:
        reasons = ["continuity_floor"]
        reasons.extend(f"continuity_signal:{signal}" for signal in continuity_signals[:3])
        return _profile("standard", reasons)
    if requested == "light":
        return _profile("light", ["explicit_profile"])
    if requested == "standard":
        return _profile("standard", ["explicit_profile"])
    if len(normalized) <= 160:
        return _profile("light", ["short_low_risk_task"])
    return _profile("standard", ["complexity_floor"])
