from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Tuple

from multi_turn.config import LLMConfig, RunConfig
from .flow import Flow
from .trajectory import TrajectoryFlow


def _ensure_dirs(output_dir: Path, trace_run_name: str) -> Dict[str, Path]:
    results_dir = output_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    approved_cases_dir = output_dir / "approved_cases"
    approved_cases_dir.mkdir(parents=True, exist_ok=True)
    trajectories_dir = output_dir / "trajectories" / trace_run_name
    trajectories_dir.mkdir(parents=True, exist_ok=True)
    return {
        "results": results_dir,
        "approved_cases": approved_cases_dir,
        "trajectories": trajectories_dir,
    }


def _load_prompt(prompt_dir: Path, category: str) -> str:
    path = prompt_dir / f"{category}.md"
    if not path.exists():
        raise FileNotFoundError(f"Missing compiled prompt file for category {category}: {path}")
    return path.read_text(encoding="utf-8")


def _result_name(category: str, index: int) -> str:
    return f"{category}_{index:04d}"


def _manifest_entry(category: str, index: int, result: Dict[str, Any], dirs: Dict[str, Path]) -> Dict[str, Any]:
    name = _result_name(category, index)
    metadata = result.get("case", {}).get("metadata", {}) if isinstance(result.get("case"), dict) else {}
    return {
        "name": name,
        "status": result.get("status"),
        "approved_case_id": result.get("case_id"),
        "category": category,
        "display_name": metadata.get("display_name"),
        "result_path": str(dirs["results"] / f"{name}.json"),
    }


def _load_manifest(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_indices(results_dir: Path, category: str) -> List[int]:
    pattern = re.compile(rf"^{re.escape(category)}_(\d{{4}})\.json$")
    indices: List[int] = []
    for path in results_dir.glob(f"{category}_*.json"):
        matched = pattern.match(path.name)
        if matched:
            indices.append(int(matched.group(1)))
    return sorted(indices)


def _build_trajectory_configs(config: RunConfig) -> Tuple[LLMConfig, LLMConfig]:
    trajectory_config = LLMConfig(
        base_url=config.trajectory_base_url,
        api_key=config.trajectory_api_key,
        model=config.trajectory_model,
        temperature=config.trajectory_temperature,
    )
    tool_executor_config = LLMConfig(
        base_url=config.tool_executor_base_url,
        api_key=config.tool_executor_api_key,
        model=config.tool_executor_model,
        temperature=config.tool_executor_temperature,
    )
    return trajectory_config, tool_executor_config


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _trajectory_task(
    flow: TrajectoryFlow,
    case_payload: Dict[str, Any],
    output_path: Path,
    ordinal: int,
    total: int,
    label: str,
    lock: threading.Lock,
    max_turns: int,
) -> Path:
    with lock:
        print(f"[trajectory][{ordinal}/{total}] start {label} -> {output_path.name}", flush=True)
    trajectory = flow.run(case_payload, max_turns=max_turns)
    output_path.write_text(json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
    with lock:
        print(f"[trajectory][{ordinal}/{total}] done {label} -> {output_path.name}", flush=True)
    return output_path


def run(config: RunConfig) -> List[Dict[str, Any]]:
    """Run generation for all configured categories."""

    if not config.prompt_dir or not config.prompt_dir.exists():
        raise FileNotFoundError("compiled_prompts directory is required for cases.py")

    pipeline = Flow(config)
    dirs = _ensure_dirs(config.output_dir, config.trace_run_name)
    manifest_path = config.output_dir / "manifest.json"
    failed_cases_path = config.output_dir / "failed_cases.jsonl"
    failed_trajectories_path = dirs["trajectories"] / "failed_trajectories.jsonl"
    manifest = _load_manifest(manifest_path) if config.resume else []

    work_items: List[Tuple[str, int, str]] = []
    for category, count in config.category_counts.items():
        start_index = 0
        remaining = count
        if config.resume:
            indices = _existing_indices(dirs["results"], category)
            if indices:
                start_index = indices[-1] + 1
            remaining = max(0, count - len(indices))
        for index in range(start_index, start_index + remaining):
            work_items.append((category, index, _result_name(category, index)))

    total_cases = len(work_items)
    trajectory_lock = threading.Lock()
    trajectory_futures: List[Tuple[Future[Path], Dict[str, Any]]] = []
    executor: ThreadPoolExecutor | None = None
    trajectory_flow: TrajectoryFlow | None = None

    if config.run_trajectory and total_cases > 0:
        trajectory_config, tool_executor_config = _build_trajectory_configs(config)
        trajectory_flow = TrajectoryFlow(trajectory_config, tool_executor_config, trajectory_style=config.trajectory_style)
        executor = ThreadPoolExecutor(max_workers=config.trajectory_workers)

    for ordinal, (category, index, name) in enumerate(work_items, start=1):
        prompt = _load_prompt(config.prompt_dir, category)
        result: Dict[str, Any] | None = None
        attempts = config.case_retry_limit + 1

        for attempt in range(1, attempts + 1):
            try:
                print(f"[case][{ordinal}/{total_cases}] start {name} (attempt {attempt}/{attempts})", flush=True)
                result = pipeline.run(prompt, category, index)
                print(f"[case][{ordinal}/{total_cases}] done {name}", flush=True)
                break
            except Exception as exc:
                print(f"[case][{ordinal}/{total_cases}] failed {name} on attempt {attempt}/{attempts}: {exc}", flush=True)
                if attempt == attempts:
                    record = {
                        "name": name,
                        "category": category,
                        "index": index,
                        "attempts": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    _append_jsonl(failed_cases_path, record)
                    if not config.continue_on_error:
                        raise
                else:
                    time.sleep(min(5.0, float(attempt)))

        if result is None:
            continue

        result_path = dirs["results"] / f"{name}.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest.append(_manifest_entry(category, index, result, dirs))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        if executor is None or trajectory_flow is None:
            continue

        case_payload = result.get("case", {})
        case_id = result.get("case_id")
        if not case_payload or case_id is None:
            continue
        output_path = dirs["trajectories"] / f"case_{case_id}_trajectory.json"
        future = executor.submit(
            _trajectory_task,
            trajectory_flow,
            case_payload,
            output_path,
            ordinal,
            total_cases,
            name,
            trajectory_lock,
            config.trajectory_max_turns,
        )
        trajectory_futures.append(
            (
                future,
                {
                    "name": name,
                    "category": category,
                    "index": index,
                    "case_id": case_id,
                    "output_path": str(output_path),
                    "ordinal": ordinal,
                    "total": total_cases,
                },
            )
        )

    if executor is not None:
        executor.shutdown(wait=False)

    for future, meta in trajectory_futures:
        try:
            future.result()
        except Exception as exc:
            with trajectory_lock:
                print(
                    f"[trajectory][{meta['ordinal']}/{meta['total']}] failed {meta['name']}: {exc}",
                    flush=True,
                )
            record = {
                **meta,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            _append_jsonl(failed_trajectories_path, record)
            if not config.continue_on_error:
                raise

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
