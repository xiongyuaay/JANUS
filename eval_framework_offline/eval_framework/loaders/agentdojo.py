"""AgentDojo trajectory loader.

Expected on-disk layout (under ``data_root``):

    <data_root>/agentdojo/<on_disk_split>/<model>/<suite>/<user_task>/<attack>/<injection>.json

If the loader is pointed directly at an agentdojo directory, ``<data_root>``
may also equal the benchmark root (i.e. without the trailing ``agentdojo/``).

Each file follows AgentDojo's run format with at least:
``messages`` (OpenAI chat format), ``suite_name``, ``user_task_id``,
``injection_task_id`` (null for benign), ``utility``, ``security``.

Important nuance: the benign split contains both regular ``user_task_*``
runs and direct ``injection_task_*`` control runs executed as ordinary
user requests. Under the framework convention, ``security=True`` means
an unsafe objective succeeded. After the data repair, safe benign
``user_task_*`` runs therefore carry ``security=False`` while direct
``injection_task_*`` controls carry ``security=True`` and are excluded
from benign utility denominators.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from ..schema import Split, Step, ToolCall, Trajectory
from .base import TrajectoryLoader
from .static_trace import has_static_layout, parse_messages, static_models

logger = logging.getLogger(__name__)


def _parse_messages(raw_messages: list[dict[str, Any]]) -> tuple[Step, ...]:
    steps: list[Step] = []
    for idx, m in enumerate(raw_messages):
        role = m.get("role")
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        if role == "assistant":
            raw_tc = m.get("tool_calls") or []
            tcs = (
                tuple(
                    ToolCall(
                        name=str(tc.get("function", "")),
                        arguments=dict(tc.get("args") or {}),
                        call_id=tc.get("id"),
                    )
                    for tc in raw_tc
                )
                or None
            )
            steps.append(Step(index=idx, role="assistant", content=content, tool_calls=tcs))
        elif role == "tool":
            nested = m.get("tool_call") or {}
            steps.append(
                Step(
                    index=idx,
                    role="tool",
                    content=content,
                    tool_name=nested.get("function"),
                    tool_call_id=m.get("tool_call_id"),
                )
            )
        elif role in ("system", "user"):
            steps.append(Step(index=idx, role=role, content=content))
        else:
            logger.debug("agentdojo: skipping role %r at index %d", role, idx)
    return tuple(steps)


class AgentDojoLoader(TrajectoryLoader):
    name = "agentdojo"
    has_model_dim = True
    # dir_name defaults to name; split_map is the identity for AgentDojo.

    def available_models(self, data_root: Path) -> list[str]:
        if has_static_layout(data_root, self.name):
            return static_models(data_root, self.name)
        root = self.resolved_root(data_root)
        models: set[str] = set()
        for on_disk in self.split_map:
            split_dir = root / on_disk
            if not split_dir.is_dir():
                continue
            for p in split_dir.iterdir():
                if p.is_dir():
                    models.add(p.name)
        return sorted(models)

    def iter_trajectories(
        self,
        data_root: Path,
        *,
        splits: Sequence[Split] = ("benign", "adversarial"),
        models: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Trajectory]:
        if has_static_layout(data_root, self.name):
            yield from self._iter_static(data_root, splits=splits, models=models, limit=limit)
            return

        root = self.resolved_root(data_root)
        wanted_models = set(models) if models else None
        count = 0

        on_disk_targets: list[tuple[Split, str]] = []
        for canonical in splits:
            for on_disk in sorted(self.split_map):
                if self.split_map[on_disk] == canonical:
                    on_disk_targets.append((canonical, on_disk))

        for canonical_split, on_disk_name in on_disk_targets:
            split_dir = root / on_disk_name
            if not split_dir.is_dir():
                logger.info("agentdojo: missing split dir %s", split_dir)
                continue
            for model_dir in sorted(p for p in split_dir.iterdir() if p.is_dir()):
                model = model_dir.name
                if wanted_models is not None and model not in wanted_models:
                    continue
                for json_path in sorted(model_dir.rglob("*.json")):
                    try:
                        raw = json.loads(json_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        logger.warning("agentdojo: failed to read %s: %s", json_path, exc)
                        continue
                    if not isinstance(raw, dict) or "messages" not in raw:
                        continue

                    rel = json_path.relative_to(model_dir).with_suffix("")
                    tid = f"{model}/{rel.as_posix()}"

                    steps = _parse_messages(raw.get("messages") or [])
                    if not steps:
                        continue

                    yield Trajectory(
                        id=tid,
                        benchmark=self.name,
                        split=canonical_split,
                        steps=steps,
                        source_path=str(json_path),
                        model=model,
                        utility=raw.get("utility"),
                        security=raw.get("security"),
                        extra={
                            "on_disk_split": on_disk_name,
                            "suite_name": raw.get("suite_name"),
                            "user_task_id": raw.get("user_task_id"),
                            "injection_task_id": raw.get("injection_task_id"),
                            "attack_type": raw.get("attack_type"),
                        },
                    )
                    count += 1
                    if limit is not None and count >= limit:
                        return

    def _iter_static(
        self,
        data_root: Path,
        *,
        splits: Sequence[Split],
        models: Optional[Sequence[str]],
        limit: Optional[int],
    ) -> Iterator[Trajectory]:
        wanted_models = set(models) if models else None
        wanted_splits = set(splits)
        count = 0
        split_map: dict[str, Split] = {"benign": "benign", "attack": "adversarial"}
        for model_dir in sorted((data_root / self.name).iterdir()):
            if not model_dir.is_dir():
                continue
            model = model_dir.name
            if wanted_models is not None and model not in wanted_models:
                continue
            for canonical_split in splits:
                if canonical_split not in wanted_splits:
                    continue
                for on_disk in sorted(split_map):
                    if split_map[on_disk] != canonical_split:
                        continue
                    logs_dir = model_dir / on_disk / "logs"
                    if not logs_dir.is_dir():
                        continue
                    for json_path in sorted(logs_dir.rglob("*.json")):
                        raw = json.loads(json_path.read_text(encoding="utf-8"))
                        if not isinstance(raw, dict):
                            continue
                        steps = parse_messages(raw.get("messages"))
                        if not steps:
                            continue
                        rel = json_path.relative_to(logs_dir).with_suffix("")
                        yield Trajectory(
                            id=f"{model}/{on_disk}/{rel.as_posix()}",
                            benchmark=self.name,
                            split=canonical_split,
                            steps=steps,
                            source_path=str(json_path),
                            model=model,
                            utility=raw.get("utility"),
                            security=raw.get("security"),
                            extra={
                                "on_disk_split": on_disk,
                                "suite_name": raw.get("suite_name"),
                                "pipeline_name": raw.get("pipeline_name"),
                                "user_task_id": raw.get("user_task_id"),
                                "injection_task_id": raw.get("injection_task_id"),
                                "attack_type": raw.get("attack_type"),
                                "annotation": raw.get("annotation"),
                            },
                        )
                        count += 1
                        if limit is not None and count >= limit:
                            return
