from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


StepLabel = Literal["safe", "potential_unsafe", "unsafe"]
Split = Literal["benign", "adversarial"]
StepRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: Optional[str] = None


@dataclass(frozen=True)
class Step:
    index: int
    role: StepRole
    content: str
    tool_calls: Optional[tuple[ToolCall, ...]] = None
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass(frozen=True)
class Trajectory:
    """Canonical trajectory.

    `utility` = original task-completion bool for the task that was
    explicitly requested in the trajectory.

    `security` = unsafe-objective success under the framework convention:
        True  -> an unsafe / security-sensitive objective succeeded
                 (e.g. prompt injection success, direct malicious request,
                 or other annotated unsafe behaviour),
        False -> the trajectory is safe,
        None  -> the source data has not yet been annotated.
    """

    id: str
    benchmark: str
    split: Split
    steps: tuple[Step, ...]
    source_path: str
    model: Optional[str] = None
    utility: Optional[bool] = None
    security: Optional[bool] = None
    extra: dict[str, Any] = field(default_factory=dict)
