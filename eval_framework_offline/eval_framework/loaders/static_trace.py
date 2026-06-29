"""Loaders for unified static trace outputs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from ..schema import Split, Step, ToolCall, Trajectory
from .base import TrajectoryLoader


def has_static_layout(data_root: Path, benchmark_dir: str) -> bool:
    root = data_root / benchmark_dir
    if not root.is_dir():
        return False
    for model_dir in root.iterdir():
        if not model_dir.is_dir():
            continue
        if (model_dir / "trajectories.jsonl").is_file():
            return True
        for child in model_dir.iterdir():
            if not child.is_dir():
                continue
            if (child / "trajectories.jsonl").is_file() or (child / "logs").is_dir():
                return True
    return False


def static_models(data_root: Path, benchmark_dir: str) -> list[str]:
    root = data_root / benchmark_dir
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                content = item.get("content", item.get("text", ""))
                parts.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False)


def tool_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"_raw": value}
    return {}


def parse_tool_calls(raw_calls: Any) -> Optional[tuple[ToolCall, ...]]:
    if not raw_calls:
        return None
    calls: list[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function")
        if isinstance(fn, dict):
            name = fn.get("name") or ""
            args = tool_args(fn.get("arguments"))
        else:
            name = fn or raw.get("tool_name") or raw.get("name") or ""
            args = tool_args(raw.get("args") if "args" in raw else raw.get("arguments"))
        calls.append(ToolCall(name=str(name), arguments=args, call_id=raw.get("id")))
    return tuple(calls) if calls else None


def parse_messages(raw_messages: Any) -> tuple[Step, ...]:
    if not isinstance(raw_messages, list):
        return ()
    steps: list[Step] = []
    for idx, msg in enumerate(raw_messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = text_content(msg.get("content"))
        if role == "assistant":
            steps.append(Step(idx, "assistant", content, parse_tool_calls(msg.get("tool_calls"))))
        elif role == "tool":
            tool_call = msg.get("tool_call") or {}
            steps.append(
                Step(
                    idx,
                    "tool",
                    content,
                    tool_name=tool_call.get("function") or msg.get("name"),
                    tool_call_id=msg.get("tool_call_id"),
                )
            )
        elif role in ("system", "user"):
            steps.append(Step(idx, role, content))
    return tuple(steps)


def record_id(raw: dict[str, Any], fallback: str) -> str:
    for key in ("case_id", "id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def bool_from_label(label: Any) -> Optional[bool]:
    if label == "unsafe":
        return True
    if label == "safe":
        return False
    return None


class StaticTraceJSONLLoader(TrajectoryLoader):
    """Read ``<benchmark>/<model>/<split>/trajectories.jsonl`` traces."""

    benchmark_dir: str = ""
    model_name: Optional[str] = None
    split_map: dict[str, Split] = {}
    default_split: Optional[Split] = None
    has_model_dim = True

    def available_models(self, data_root: Path) -> list[str]:
        return static_models(data_root, self.benchmark_dir)

    def uses_model_filter(self, data_root: Path) -> bool:
        return has_static_layout(data_root, self.benchmark_dir)

    def _extra_for(
        self,
        raw: dict[str, Any],
        *,
        on_disk_split: Optional[str],
        jsonl_path: Path,
    ) -> dict[str, Any]:
        return {
            "on_disk_split": on_disk_split,
            "metadata": raw.get("metadata"),
            "error": raw.get("error"),
            "blocked_actions": raw.get("blocked_actions"),
            "flagged_actions": raw.get("flagged_actions"),
            "annotation": raw.get("annotation"),
        }

    def _security_for(self, raw: dict[str, Any]) -> Optional[bool]:
        if "security" in raw:
            return raw.get("security")
        annotation = raw.get("annotation") or {}
        if isinstance(annotation, dict):
            label = annotation.get("label")
            if label is not None:
                return bool_from_label(label)
        return None

    def _utility_for(self, raw: dict[str, Any]) -> Optional[bool]:
        if "utility" in raw:
            return raw.get("utility")
        if "pass" in raw:
            return raw.get("pass")
        return None

    def _iter_file(
        self,
        jsonl_path: Path,
        *,
        split: Split,
        model: str,
        on_disk_split: Optional[str],
        count: int,
        limit: Optional[int],
    ) -> Iterator[tuple[Trajectory, int]]:
        for line_no, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            steps = parse_messages(raw.get("messages"))
            if not steps:
                continue
            fallback = f"{model}/{on_disk_split or jsonl_path.stem}/{line_no}"
            yield (
                Trajectory(
                    id=record_id(raw, fallback),
                    benchmark=self.name,
                    split=split,
                    steps=steps,
                    source_path=str(jsonl_path),
                    model=model,
                    utility=self._utility_for(raw),
                    security=self._security_for(raw),
                    extra=self._extra_for(raw, on_disk_split=on_disk_split, jsonl_path=jsonl_path),
                ),
                count + 1,
            )
            count += 1
            if limit is not None and count >= limit:
                return

    def iter_trajectories(
        self,
        data_root: Path,
        *,
        splits: Sequence[Split] = ("benign", "adversarial"),
        models: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Trajectory]:
        wanted_models = set(models) if models else None
        wanted_splits = set(splits)
        count = 0
        for model_dir in sorted((data_root / self.benchmark_dir).iterdir()):
            if not model_dir.is_dir():
                continue
            model = self.model_name or model_dir.name
            if wanted_models is not None and model not in wanted_models and model_dir.name not in wanted_models:
                continue
            if self.default_split is not None:
                if self.default_split not in wanted_splits:
                    continue
                jsonl_path = model_dir / "trajectories.jsonl"
                if jsonl_path.is_file():
                    for trajectory, count in self._iter_file(
                        jsonl_path,
                        split=self.default_split,
                        model=model,
                        on_disk_split=None,
                        count=count,
                        limit=limit,
                    ):
                        yield trajectory
                    if limit is not None and count >= limit:
                        return
                continue
            for split in splits:
                if split not in wanted_splits:
                    continue
                for on_disk in sorted(self.split_map):
                    if self.split_map[on_disk] != split:
                        continue
                    jsonl_path = model_dir / on_disk / "trajectories.jsonl"
                    if not jsonl_path.is_file():
                        continue
                    for trajectory, count in self._iter_file(
                        jsonl_path,
                        split=split,
                        model=model,
                        on_disk_split=on_disk,
                        count=count,
                        limit=limit,
                    ):
                        yield trajectory
                    if limit is not None and count >= limit:
                        return


class StaticASBLoader(StaticTraceJSONLLoader):
    name = "asb"
    benchmark_dir = "asb"
    split_map = {
        "normal": "benign",
        "dpi": "adversarial",
        "opi_ipi": "adversarial",
    }


class StaticAgentSafetyBenchLoader(StaticTraceJSONLLoader):
    name = "agent_safetybench"
    benchmark_dir = "agent_safetybench"
    default_split = "adversarial"

    def _security_for(self, raw: dict[str, Any]) -> Optional[bool]:
        if "security" in raw:
            return raw.get("security")
        annotation = raw.get("annotation") or {}
        if isinstance(annotation, dict):
            return bool_from_label(annotation.get("label"))
        return None


class StaticAgentLabLoader(StaticTraceJSONLLoader):
    name = "agentlab"
    benchmark_dir = "agentlab"
    split_map = {
        "memory_poisoning": "adversarial",
        "tool_chaining": "adversarial",
    }


class StaticLPSBenchLoader(StaticTraceJSONLLoader):
    name = "lps_bench"
    benchmark_dir = "lps_bench"
    default_split = "adversarial"
