from __future__ import annotations

from typing import Any, Sequence

from ..schema import Step
from .base import Guard, Verdict


class AlwaysSafeGuard(Guard):
    name = "stub_safe"

    def judge(self, history: Sequence[Step], meta: dict[str, Any]) -> Verdict:
        return Verdict(flagged=False, score=0.0, reason="always_safe")


class AlwaysUnsafeGuard(Guard):
    name = "stub_unsafe"

    def judge(self, history: Sequence[Step], meta: dict[str, Any]) -> Verdict:
        return Verdict(flagged=True, score=1.0, reason="always_unsafe")
