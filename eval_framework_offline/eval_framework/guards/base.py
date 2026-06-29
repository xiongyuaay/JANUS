from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..schema import Step, StepLabel


@dataclass
class Verdict:
    """Per-step judgement produced by a Guard.

    `flagged` is the legacy binary output kept for back-compat; `label`
    is the 3-way classification used by the mode-aware aggregator. If a
    guard only emits binary output, `label` is derived at aggregation
    time (`unsafe` if flagged else `safe`).
    """

    flagged: bool
    score: Optional[float] = None
    reason: Optional[str] = None
    label: Optional[StepLabel] = None
    raw: dict[str, Any] = field(default_factory=dict)


class Guard(ABC):
    name: str = "guard"

    @abstractmethod
    def judge(self, history: Sequence[Step], meta: dict[str, Any]) -> Verdict:
        """Judge whether the most recent step of `history` is unsafe.

        `history` is the accumulated sequence of canonical steps from index 0
        up to and including the current step `t`. Guards MUST NOT look beyond
        the tail of `history`.
        """
        raise NotImplementedError
