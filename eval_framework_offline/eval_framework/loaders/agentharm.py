"""AgentHarm ReAct-uncensor loader.

On-disk layout:

    <data_root>/agentharm_react_uncensor/{benign,harmful}/<id>.json

``harmful`` is normalised to the framework's canonical ``adversarial``
split. No per-model dimension. Files carry top-level ``utility`` /
``security`` booleans in AgentDojo polarity (security=true means the
attack succeeded / agent was unsafe).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from ..schema import Split, Trajectory
from .generic import GenericSplitLoader
from .static_trace import StaticTraceJSONLLoader, has_static_layout, static_models


class AgentHarmLoader(GenericSplitLoader):
    name = "agentharm"
    dir_name = "agentharm_react_uncensor"
    split_map = {"benign": "benign", "harmful": "adversarial"}
    has_model_dim = False

    def available_models(self, data_root: Path) -> list[str]:
        if has_static_layout(data_root, self.name):
            return static_models(data_root, self.name)
        return super().available_models(data_root)

    def uses_model_filter(self, data_root: Path) -> bool:
        return has_static_layout(data_root, self.name)

    def iter_trajectories(
        self,
        data_root: Path,
        *,
        splits: Sequence[Split] = ("benign", "adversarial"),
        models: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Trajectory]:
        if has_static_layout(data_root, self.name):
            loader = StaticTraceJSONLLoader()
            loader.name = self.name
            loader.benchmark_dir = self.name
            loader.split_map = self.split_map
            yield from loader.iter_trajectories(
                data_root,
                splits=splits,
                models=models,
                limit=limit,
            )
            return
        yield from super().iter_trajectories(
            data_root,
            splits=splits,
            models=models,
            limit=limit,
        )

    def _extra_for(
        self,
        raw: dict[str, Any],
        *,
        on_disk_split: str,
        canonical_split: Split,
        json_path: Path,
    ) -> dict[str, Any]:
        return {
            "category": raw.get("category"),
            "name": raw.get("name"),
            "grading_function": raw.get("grading_function"),
            "target_functions": raw.get("target_functions"),
            "stop_reason": raw.get("stop_reason"),
        }
