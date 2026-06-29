"""Step-level replay of a single trajectory through a single guard."""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

from .guards.base import Guard, Verdict
from .schema import Trajectory

logger = logging.getLogger(__name__)


def replay(
    trajectory: Trajectory,
    guard: Guard,
    *,
    stop_on_first_flag: bool = False,
    judge_prefix_ratio: Optional[float] = None,
    meta_extra: Optional[dict[str, Any]] = None,
) -> list[Verdict]:
    """Run `guard` over every prefix of `trajectory.steps`.

    Returns a list of per-step verdicts. `judge_prefix_ratio` truncates the
    replay to the first ratio of the full trajectory; `stop_on_first_flag=True`
    truncates earlier at the first flagged step.
    """
    base_meta = {
        "trajectory_id": trajectory.id,
        "benchmark": trajectory.benchmark,
        "split": trajectory.split,
        "model": trajectory.model,
        "source_path": trajectory.source_path,
    }
    if meta_extra:
        base_meta.update(meta_extra)

    verdicts: list[Verdict] = []
    steps = trajectory.steps
    visible_cap = len(steps)
    if judge_prefix_ratio is not None:
        if judge_prefix_ratio <= 0 or judge_prefix_ratio > 1:
            raise ValueError("judge_prefix_ratio must be in (0, 1]")
        visible_cap = max(1, math.floor(len(steps) * judge_prefix_ratio))
    for t in range(visible_cap):
        history = steps[: t + 1]
        meta = dict(base_meta)
        meta["step_index"] = history[-1].index
        if judge_prefix_ratio is not None:
            meta["visible_prefix_steps"] = visible_cap
            meta["judge_prefix_ratio"] = judge_prefix_ratio
        try:
            v = guard.judge(history, meta)
        except Exception as exc:  # noqa: BLE001 — keep the batch moving
            logger.exception("guard %s crashed on %s step %d", guard.name, trajectory.id, t)
            v = Verdict(flagged=False, reason=f"guard_exception: {exc}")
        verdicts.append(v)
        if stop_on_first_flag and v.flagged:
            break
    return verdicts
