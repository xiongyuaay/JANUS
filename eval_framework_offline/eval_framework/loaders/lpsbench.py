"""LPS-Bench trajectory loader.

Expected on-disk layout (under ``data_root``):

    <data_root>/lps_bench_uncensor/{benign,adversarial}/<category>/<case_id>.json

The uncensor variant currently ships adversarial cases only (``benign``
subfolder exists but may be empty). Raw JSON has top-level ``id``,
``chat_history`` (mixed role-based and ChatCompletion-choice wrappers),
and — once annotated — ``utility`` / ``security`` booleans under the
framework's AgentDojo polarity.

No per-model dimension at this layer.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from ..schema import Split, Step, ToolCall, Trajectory
from .base import TrajectoryLoader
from .static_trace import StaticLPSBenchLoader, has_static_layout, static_models

logger = logging.getLogger(__name__)


def _unwrap_turn(turn: dict[str, Any]) -> Optional[dict[str, Any]]:
    if "choices" in turn and isinstance(turn["choices"], list) and turn["choices"]:
        msg = turn["choices"][0].get("message") or {}
        return {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls"),
        }
    if "role" in turn:
        return {
            "role": turn["role"],
            "content": turn.get("content") or "",
            "name": turn.get("name"),
            "tool_call_id": turn.get("tool_call_id"),
            "tool_calls": turn.get("tool_calls"),
        }
    return None


def _parse_tool_calls(raw_tc: Any) -> Optional[tuple[ToolCall, ...]]:
    if not raw_tc:
        return None
    out: list[ToolCall] = []
    for tc in raw_tc:
        fn = tc.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else str(fn)
        args_raw = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        out.append(ToolCall(name=str(name or ""), arguments=args, call_id=tc.get("id")))
    return tuple(out) if out else None


def _parse_chat_history(chat_history: list[dict[str, Any]]) -> tuple[Step, ...]:
    steps: list[Step] = []
    idx = 0
    for turn in chat_history:
        flat = _unwrap_turn(turn)
        if flat is None:
            continue
        role = flat["role"]
        content = flat.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if role == "assistant":
            tcs = _parse_tool_calls(flat.get("tool_calls"))
            steps.append(Step(index=idx, role="assistant", content=content, tool_calls=tcs))
        elif role == "tool":
            steps.append(
                Step(
                    index=idx,
                    role="tool",
                    content=content,
                    tool_name=flat.get("name"),
                    tool_call_id=flat.get("tool_call_id"),
                )
            )
        elif role in ("system", "user"):
            steps.append(Step(index=idx, role=role, content=content))
        else:
            logger.warning("lpsbench: skipping unknown role %r at chat index %d", role, idx)
            continue
        idx += 1
    return tuple(steps)


class LPSBenchLoader(TrajectoryLoader):
    name = "lps_bench"
    dir_name = "lps_bench_uncensor"
    has_model_dim = False

    def available_models(self, data_root: Path) -> list[str]:
        if has_static_layout(data_root, self.name):
            return static_models(data_root, self.name)
        return []

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
            yield from StaticLPSBenchLoader().iter_trajectories(
                data_root,
                splits=splits,
                models=models,
                limit=limit,
            )
            return

        if models:
            logger.debug("lpsbench: --models ignored (benchmark has no model dimension)")
        root = self.resolved_root(data_root)
        count = 0

        on_disk_targets: list[tuple[Split, str]] = []
        for canonical in splits:
            for on_disk in sorted(self.split_map):
                if self.split_map[on_disk] == canonical:
                    on_disk_targets.append((canonical, on_disk))

        for canonical_split, on_disk_name in on_disk_targets:
            split_dir = root / on_disk_name
            if not split_dir.is_dir():
                logger.info("lpsbench: missing split dir %s", split_dir)
                continue
            for json_path in sorted(split_dir.rglob("*.json")):
                if json_path.name.startswith("summary"):
                    continue
                try:
                    raw = json.loads(json_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("lpsbench: failed to read %s: %s", json_path, exc)
                    continue
                if not isinstance(raw, dict) or "chat_history" not in raw:
                    continue

                tid = raw.get("id")
                if not isinstance(tid, str) or not tid:
                    rel = json_path.relative_to(split_dir).with_suffix("")
                    tid = rel.as_posix()

                steps = _parse_chat_history(raw.get("chat_history") or [])
                if not steps:
                    continue

                yield Trajectory(
                    id=tid,
                    benchmark=self.name,
                    split=canonical_split,
                    steps=steps,
                    source_path=str(json_path),
                    model=raw.get("model"),
                    utility=raw.get("utility"),
                    security=raw.get("security"),
                    extra={
                        "on_disk_split": on_disk_name,
                        "category": json_path.parent.name,
                    },
                )
                count += 1
                if limit is not None and count >= limit:
                    return
