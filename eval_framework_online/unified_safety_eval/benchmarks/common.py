"""Benchmark abstractions."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..agent import Tool, UnifiedReActAgent, AgentRunResult
from ..progress import ProgressTracker, error, info, progress_iter
from ..utils import append_jsonl, ensure_dir, json_default, write_json


@dataclass
class BenchmarkCase:
    id: str
    prompt: str
    tools: dict[str, Tool]
    metadata: dict[str, Any] = field(default_factory=dict)
    initial_messages: list[dict[str, Any]] | None = None


@dataclass
class BenchmarkRunOutput:
    benchmark: str
    output_dir: Path
    status: str
    metrics: dict[str, Any]
    num_cases: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class BenchmarkAdapter:
    name: str = "benchmark"

    def __init__(self, project_root: Path, cfg: dict[str, Any], run_cfg: dict[str, Any]):
        self.project_root = project_root
        self.cfg = cfg or {}
        self.run_cfg = run_cfg or {}

    def load_cases(self) -> list[BenchmarkCase]:
        raise NotImplementedError

    def evaluate(self, results: list[tuple[BenchmarkCase, AgentRunResult]], output_dir: Path) -> dict[str, Any]:
        raise NotImplementedError

    def run(self, agent: UnifiedReActAgent, output_dir: Path, max_workers: int = 1) -> BenchmarkRunOutput:
        raise NotImplementedError


def normalize_run_specs(runs: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(runs, Mapping):
        return [(str(name), dict(spec or {})) for name, spec in runs.items()]
    specs: list[tuple[str, dict[str, Any]]] = []
    for idx, item in enumerate(runs or []):
        spec = dict(item or {})
        name = str(spec.pop("name", f"run_{idx + 1}"))
        specs.append((name, spec))
    return specs


def run_react_cases(
    *,
    adapter: BenchmarkAdapter,
    agent: UnifiedReActAgent,
    output_dir: Path,
    max_workers: int,
) -> BenchmarkRunOutput:
    ensure_dir(output_dir)
    try:
        cases = adapter.load_cases()
    except FileNotFoundError as exc:
        error(f"[error] {adapter.name}/{output_dir.name}: missing data: {exc}")
        metrics = {"status": "skipped_missing_data", "reason": str(exc)}
        write_json(output_dir / "metrics.json", metrics)
        return BenchmarkRunOutput(adapter.name, output_dir, "skipped_missing_data", metrics, 0)
    except Exception as exc:
        error(f"[error] {adapter.name}/{output_dir.name}: load failed: {type(exc).__name__}: {exc}")
        metrics = {"status": "load_failed", "reason": str(exc)}
        write_json(output_dir / "metrics.json", metrics)
        return BenchmarkRunOutput(adapter.name, output_dir, "load_failed", metrics, 0)

    limit = adapter.run_cfg.get("limit")
    if limit is not None and str(limit).lower() not in {"", "none", "null"}:
        cases = cases[: int(limit)]

    results: list[tuple[BenchmarkCase, AgentRunResult]] = []
    traj_path = output_dir / "trajectories.jsonl"
    resumed = bool(adapter.run_cfg.get("resume", False))
    rerun_errors = bool(adapter.run_cfg.get("rerun_errors", False))
    rerun_error_patterns = _normalize_error_patterns(adapter.run_cfg.get("rerun_error_patterns"))
    # Resume: keep prior trajectories.jsonl and rebuild the in-memory results
    # from it; remaining cases are the ones whose IDs are not yet reusable.
    done_ids: set[str] = set()
    retry_ids: set[str] = set()
    if resumed and traj_path.exists():
        existing_rows = _latest_trajectory_rows(_read_trajectory_rows(traj_path))
        reusable_rows: list[dict[str, Any]] = []
        for row in existing_rows:
            case_id = str(row.get("case_id", ""))
            if not case_id:
                continue
            if rerun_errors and _row_has_rerunnable_error(row, rerun_error_patterns):
                retry_ids.add(case_id)
                continue
            done_ids.add(case_id)
            reusable_rows.append(row)
        # Rebuild minimal pairs for the metrics aggregator.
        results.extend(_pairs_from_rows(reusable_rows, cases))
        if rerun_errors and retry_ids:
            _write_trajectory_rows(traj_path, reusable_rows)
    elif traj_path.exists() and not adapter.run_cfg.get("append", False):
        traj_path.unlink()

    pending_cases = [c for c in cases if c.id not in done_ids]
    adapter.run_cfg.setdefault("_rerun_case_ids_by_output", {})[str(output_dir)] = set(retry_ids)
    if resumed and done_ids:
        from ..progress import info as _pinfo
        if retry_ids:
            _pinfo(
                f"{adapter.name}/{output_dir.name}: resume — {len(done_ids)} reusable, "
                f"{len(retry_ids)} error case(s) queued for rerun, "
                f"{len(pending_cases)} pending"
            )
        else:
            _pinfo(
                f"{adapter.name}/{output_dir.name}: resume — {len(done_ids)} done, "
                f"{len(pending_cases)} remaining"
            )
    elif resumed and retry_ids:
        from ..progress import info as _pinfo
        _pinfo(
            f"{adapter.name}/{output_dir.name}: resume — "
            f"{len(retry_ids)} error case(s) queued for rerun, "
            f"{len(pending_cases)} pending"
        )

    def _run_case(case: BenchmarkCase) -> tuple[BenchmarkCase, AgentRunResult]:
        result = agent.run_case(
            case_id=case.id,
            query=case.prompt,
            tools=case.tools,
            initial_messages=case.initial_messages,
        )
        if result.error:
            error(f"[error] {adapter.name}/{output_dir.name}: case {case.id} error: {result.error}")
        return case, result

    desc = f"{adapter.name}/{output_dir.name}"
    if max_workers <= 1:
        for case in progress_iter(pending_cases, total=len(pending_cases), desc=desc):
            pair = _run_case(case)
            results.append(pair)
            append_jsonl(traj_path, _serialize_pair(pair))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_case, case): case for case in pending_cases}
            with ProgressTracker(total=len(pending_cases), desc=desc) as bar:
                for fut in as_completed(futures):
                    pair = fut.result()
                    results.append(pair)
                    append_jsonl(traj_path, _serialize_pair(pair))
                    bar.update(1)

    metrics = adapter.evaluate(results, output_dir)
    metrics.setdefault("status", "completed")
    metrics.setdefault("benchmark", adapter.name)
    metrics.setdefault("num_cases", len(cases))
    write_json(output_dir / "metrics.json", metrics)
    return BenchmarkRunOutput(adapter.name, output_dir, "completed", metrics, len(cases))


def _serialize_pair(pair: tuple[BenchmarkCase, AgentRunResult]) -> dict[str, Any]:
    case, result = pair
    return {
        "case_id": case.id,
        "prompt": case.prompt,
        "metadata": case.metadata,
        "final_answer": result.final_answer,
        "messages": result.messages,
        "tool_calls": result.tool_calls,
        "blocked_actions": result.blocked_actions,
        "flagged_actions": getattr(result, "flagged_actions", []),
        "error": result.error,
    }


def _normalize_error_patterns(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()] if value else []
    return [str(item).lower() for item in value if str(item)]


def _row_has_rerunnable_error(row: dict[str, Any], patterns: list[str]) -> bool:
    error = row.get("error") or row.get("run_error")
    if not error:
        return False
    error_text = str(error).lower()
    if not patterns:
        return True
    return any(pattern in error_text for pattern in patterns)


def _read_trajectory_rows(traj_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in traj_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _latest_trajectory_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for idx, row in enumerate(rows):
        case_id = str(row.get("case_id", ""))
        if not case_id:
            continue
        latest[case_id] = row
        positions[case_id] = idx
    return [latest[case_id] for case_id in sorted(latest, key=lambda cid: positions[cid])]


def _write_trajectory_rows(traj_path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(traj_path.parent)
    with traj_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=json_default) + "\n")


def _load_existing_pairs(
    traj_path: Path,
    cases: list[BenchmarkCase],
) -> list[tuple[BenchmarkCase, AgentRunResult]]:
    """Re-hydrate `(case, AgentRunResult)` pairs from a trajectories.jsonl so
    that, on resume, the metrics aggregator sees previously-completed cases."""
    return _pairs_from_rows(_read_trajectory_rows(traj_path), cases)


def _pairs_from_rows(
    rows: list[dict[str, Any]],
    cases: list[BenchmarkCase],
) -> list[tuple[BenchmarkCase, AgentRunResult]]:
    """Re-hydrate `(case, AgentRunResult)` pairs from trajectory rows so
    that the metrics aggregator sees previously-completed cases."""
    case_by_id = {c.id: c for c in cases}
    pairs: list[tuple[BenchmarkCase, AgentRunResult]] = []
    for row in rows:
        case = case_by_id.get(str(row.get("case_id", "")))
        if case is None:
            continue
        result = AgentRunResult(
            case_id=str(row.get("case_id", "")),
            final_answer=str(row.get("final_answer") or ""),
            messages=list(row.get("messages") or []),
            tool_calls=list(row.get("tool_calls") or []),
            blocked_actions=list(row.get("blocked_actions") or []),
            flagged_actions=list(row.get("flagged_actions") or []),
            logs="",
            error=row.get("error"),
        )
        pairs.append((case, result))
    return pairs
