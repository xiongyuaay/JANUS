"""Generic split-based loader used for benchmarks with a flat
``<split_dir>/[<model>/]<...>/*.json`` layout.

The actual on-disk directory name is given by ``dir_name`` (defaulting to
``name``). The subfolders under it are enumerated via ``split_map`` so
benchmarks with non-standard subfolder names (``harmful`` instead of
``adversarial``, or ASB's ``clean``/``DPI``/``MP``/``OPI``) can be
normalised to the framework's canonical ``benign`` / ``adversarial``
splits without bespoke code.

Each JSON is expected to expose either ``messages`` (OpenAI chat format)
or ``chat_history`` (LPS-Bench-style), plus optional top-level
``utility`` / ``security`` booleans. Files without a recognisable
message list are skipped.

Subclasses may override ``_extra_for`` to attach benchmark-specific
metadata (e.g. ASB's ``attack_method``) to ``Trajectory.extra``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from ..schema import Split, Step, ToolCall, Trajectory
from .base import TrajectoryLoader

logger = logging.getLogger(__name__)


# ----- message parsing -----------------------------------------------------


def _parse_tool_calls(raw_tc: Any) -> Optional[tuple[ToolCall, ...]]:
    if not raw_tc:
        return None
    out: list[ToolCall] = []
    for tc in raw_tc:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if isinstance(fn, dict):
            name = fn.get("name") or ""
            args_raw = fn.get("arguments")
        else:
            name = fn if isinstance(fn, str) else tc.get("name", "")
            args_raw = tc.get("args") if "args" in tc else tc.get("arguments")
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


def _message_to_step(idx: int, msg: dict[str, Any]) -> Optional[Step]:
    role = msg.get("role")
    content = msg.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    if role == "assistant":
        return Step(idx, "assistant", content, _parse_tool_calls(msg.get("tool_calls")))
    if role == "tool":
        return Step(
            idx,
            "tool",
            content,
            tool_name=msg.get("name") or (msg.get("tool_call") or {}).get("function"),
            tool_call_id=msg.get("tool_call_id"),
        )
    if role in ("system", "user"):
        return Step(idx, role, content)
    return None


def _extract_steps(raw: dict[str, Any]) -> tuple[Step, ...]:
    messages = raw.get("messages") or raw.get("chat_history") or []
    if not isinstance(messages, list):
        return ()
    out: list[Step] = []
    idx = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        # Unwrap OpenAI ChatCompletion choice wrappers (LPS-Bench style).
        if "choices" in m and isinstance(m["choices"], list) and m["choices"]:
            inner = m["choices"][0].get("message") or {}
            m = {
                "role": inner.get("role", "assistant"),
                "content": inner.get("content") or "",
                "tool_calls": inner.get("tool_calls"),
            }
        step = _message_to_step(idx, m)
        if step is None:
            continue
        out.append(step)
        idx += 1
    return tuple(out)


# ----- loader --------------------------------------------------------------


class GenericSplitLoader(TrajectoryLoader):
    """Shared walk for any ``<dir_name>/<split_dir>/[<model>/]...`` layout."""

    has_model_dim: bool = False

    def available_models(self, data_root: Path) -> list[str]:
        if not self.has_model_dim:
            return []
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

    # Subclasses override to enrich Trajectory.extra with benchmark-specific
    # fields pulled from the raw JSON.
    def _extra_for(
        self,
        raw: dict[str, Any],
        *,
        on_disk_split: str,
        canonical_split: Split,
        json_path: Path,
    ) -> dict[str, Any]:
        return {}

    def iter_trajectories(
        self,
        data_root: Path,
        *,
        splits: Sequence[Split] = ("benign", "adversarial"),
        models: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Trajectory]:
        root = self.resolved_root(data_root)
        wanted_models = set(models) if models else None
        count = 0

        # Iterate in the caller-supplied split order so `limit` stays
        # predictable (default benign -> adversarial). For each canonical
        # split, walk its on-disk subfolders in sorted name order.
        on_disk_targets: list[tuple[Split, str]] = []
        for canonical in splits:
            for on_disk in sorted(self.split_map):
                if self.split_map[on_disk] == canonical:
                    on_disk_targets.append((canonical, on_disk))

        for canonical_split, on_disk_name in on_disk_targets:
            split_dir = root / on_disk_name
            if not split_dir.is_dir():
                logger.info(
                    "%s: missing split dir %s (maps to %s)",
                    self.name, split_dir, canonical_split,
                )
                continue

            if self.has_model_dim:
                search_roots = [
                    p for p in sorted(split_dir.iterdir())
                    if p.is_dir() and (wanted_models is None or p.name in wanted_models)
                ]
            else:
                search_roots = [split_dir]

            for search_root in search_roots:
                model = search_root.name if self.has_model_dim else None
                for json_path in sorted(search_root.rglob("*.json")):
                    if json_path.name.startswith("summary") or json_path.name == "sampling_meta.json":
                        continue
                    try:
                        raw = json.loads(json_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        logger.warning("%s: failed to read %s: %s", self.name, json_path, exc)
                        continue
                    if not isinstance(raw, dict):
                        continue

                    steps = _extract_steps(raw)
                    if not steps:
                        continue

                    rel = json_path.relative_to(search_root).with_suffix("")
                    tid = raw.get("id")
                    if not isinstance(tid, str) or not tid:
                        prefix = on_disk_name if model is None else f"{model}/{on_disk_name}"
                        tid = f"{prefix}/{rel.as_posix()}"

                    extra = {"on_disk_split": on_disk_name}
                    extra.update(
                        self._extra_for(
                            raw,
                            on_disk_split=on_disk_name,
                            canonical_split=canonical_split,
                            json_path=json_path,
                        )
                    )

                    yield Trajectory(
                        id=tid,
                        benchmark=self.name,
                        split=canonical_split,
                        steps=steps,
                        source_path=str(json_path),
                        model=model or raw.get("model"),
                        utility=raw.get("utility"),
                        security=raw.get("security"),
                        extra=extra,
                    )
                    count += 1
                    if limit is not None and count >= limit:
                        return
