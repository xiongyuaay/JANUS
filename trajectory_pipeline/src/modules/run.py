from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from config import LLMConfig, RunConfig
from .flow import Flow
from .trajectory import TrajectoryFlow, to_eval_format


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


def _resume_state(config: RunConfig, dirs: Dict[str, Path]) -> Tuple[Dict[str, int], Dict[str, int]]:
    done: Dict[str, int] = {}
    next_indices: Dict[str, int] = {}
    for category in config.category_counts:
        indices = _existing_indices(dirs["results"], category) if config.resume else []
        done[category] = len(indices)
        next_indices[category] = indices[-1] + 1 if indices else 0
    return done, next_indices


def _build_sequential_items(config: RunConfig, dirs: Dict[str, Path]) -> List[Tuple[str, int, str]]:
    done, next_indices = _resume_state(config, dirs)
    items: List[Tuple[str, int, str]] = []
    for category, count in config.category_counts.items():
        remaining = max(0, count - done[category])
        start_index = next_indices[category]
        for index in range(start_index, start_index + remaining):
            items.append((category, index, _result_name(category, index)))
    return items


def _build_proportional_items(config: RunConfig, dirs: Dict[str, Path]) -> List[Tuple[str, int, str]]:
    done, next_indices = _resume_state(config, dirs)
    targets = dict(config.category_counts)
    remaining = {category: max(0, targets[category] - done[category]) for category in targets}
    total_target = sum(targets.values())
    total_remaining = sum(remaining.values())
    total_done = sum(done.values())
    items: List[Tuple[str, int, str]] = []

    for offset in range(total_remaining):
        next_total = total_done + offset + 1
        selected = ""
        selected_deficit = float("-inf")
        for category in targets:
            if remaining[category] <= 0:
                continue
            target_count = targets[category] * next_total / total_target
            deficit = target_count - done[category]
            if deficit > selected_deficit:
                selected = category
                selected_deficit = deficit
        index = next_indices[selected]
        items.append((selected, index, _result_name(selected, index)))
        done[selected] += 1
        next_indices[selected] += 1
        remaining[selected] -= 1

    return items


def _build_work_items(config: RunConfig, dirs: Dict[str, Path]) -> List[Tuple[str, int, str]]:
    if config.case_schedule == "proportional":
        return _build_proportional_items(config, dirs)
    return _build_sequential_items(config, dirs)


def _build_trajectory_configs(config: RunConfig) -> Tuple[LLMConfig, LLMConfig, LLMConfig]:
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
    reviewer_config = LLMConfig(
        base_url=config.reviewer_base_url,
        api_key=config.reviewer_api_key,
        model=config.reviewer_model,
        temperature=config.reviewer_temperature,
    )
    return trajectory_config, tool_executor_config, reviewer_config


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
    eval_record = to_eval_format(trajectory, case_payload)
    output_path.write_text(json.dumps(eval_record, ensure_ascii=False, indent=2), encoding="utf-8")
    with lock:
        print(f"[trajectory][{ordinal}/{total}] done {label} -> {output_path.name}", flush=True)
    return output_path


def _wrap_repo_with_lock(pipeline: Flow) -> None:
    """Make Repo.save_case (id-allocation + write) atomic across threads."""

    repo = pipeline.repository
    repo_lock = threading.Lock()
    original_save = repo.save_case

    def locked_save(case: Dict[str, Any]) -> int:
        with repo_lock:
            return original_save(case)

    repo.save_case = locked_save  # type: ignore[method-assign]


def _generate_case(
    pipeline: Flow,
    prompt: str,
    category: str,
    index: int,
    name: str,
    ordinal: int,
    total_cases: int,
    attempts: int,
    failed_cases_path: Path,
    io_lock: threading.Lock,
    continue_on_error: bool,
) -> Dict[str, Any] | None:
    for attempt in range(1, attempts + 1):
        try:
            with io_lock:
                print(f"[case][{ordinal}/{total_cases}] start {name} (attempt {attempt}/{attempts})", flush=True)
            result = pipeline.run(prompt, category, index)
            with io_lock:
                print(f"[case][{ordinal}/{total_cases}] done {name}", flush=True)
            return result
        except Exception as exc:
            with io_lock:
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
                with io_lock:
                    _append_jsonl(failed_cases_path, record)
                if not continue_on_error:
                    raise
            else:
                time.sleep(min(5.0, float(attempt)))
    return None


def run(config: RunConfig) -> List[Dict[str, Any]]:
    """Run generation for all configured categories."""

    if not config.prompt_dir or not config.prompt_dir.exists():
        raise FileNotFoundError("compiled_prompts directory is required for cases.py")

    pipeline = Flow(config)
    _wrap_repo_with_lock(pipeline)
    dirs = _ensure_dirs(config.output_dir, config.trace_run_name)
    manifest_path = config.output_dir / "manifest.json"
    failed_cases_path = config.output_dir / "failed_cases.jsonl"
    failed_trajectories_path = dirs["trajectories"] / "failed_trajectories.jsonl"
    manifest = _load_manifest(manifest_path) if config.resume else []

    work_items = _build_work_items(config, dirs)
    total_cases = len(work_items)

    io_lock = threading.Lock()
    manifest_lock = threading.Lock()
    trajectory_lock = threading.Lock()
    trajectory_futures_lock = threading.Lock()
    trajectory_futures: List[Tuple[Future[Path], Dict[str, Any]]] = []
    trajectory_executor: ThreadPoolExecutor | None = None
    trajectory_flow: TrajectoryFlow | None = None

    if config.run_trajectory and total_cases > 0:
        trajectory_config, tool_executor_config, reviewer_config = _build_trajectory_configs(config)
        trajectory_flow = TrajectoryFlow(
            trajectory_config=trajectory_config,
            tool_executor_config=tool_executor_config,
            reviewer_config=reviewer_config,
            trajectory_style=config.trajectory_style,
            enable_rejection_sampling=config.enable_rejection_sampling,
            max_rejection_retries=config.max_rejection_retries,
        )
        trajectory_executor = ThreadPoolExecutor(max_workers=config.trajectory_workers)

    def _process_one(ordinal: int, category: str, index: int, name: str) -> None:
        prompt = _load_prompt(config.prompt_dir, category)
        result = _generate_case(
            pipeline=pipeline,
            prompt=prompt,
            category=category,
            index=index,
            name=name,
            ordinal=ordinal,
            total_cases=total_cases,
            attempts=config.case_retry_limit + 1,
            failed_cases_path=failed_cases_path,
            io_lock=io_lock,
            continue_on_error=config.continue_on_error,
        )
        if result is None:
            return

        result_path = dirs["results"] / f"{name}.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with manifest_lock:
            manifest.append(_manifest_entry(category, index, result, dirs))
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        if trajectory_executor is None or trajectory_flow is None:
            return

        case_payload = result.get("case", {})
        case_id = result.get("case_id")
        if not case_payload or case_id is None:
            return
        output_path = dirs["trajectories"] / f"case_{case_id}_trajectory.json"
        future = trajectory_executor.submit(
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
        with trajectory_futures_lock:
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

    case_workers = max(1, min(config.case_workers, total_cases)) if total_cases > 0 else 1
    case_executor = ThreadPoolExecutor(max_workers=case_workers)
    case_futures: List[Future[None]] = [
        case_executor.submit(_process_one, ordinal, category, index, name)
        for ordinal, (category, index, name) in enumerate(work_items, start=1)
    ]

    try:
        for future in as_completed(case_futures):
            try:
                future.result()
            except Exception:
                if not config.continue_on_error:
                    for pending in case_futures:
                        pending.cancel()
                    raise
    finally:
        case_executor.shutdown(wait=True)

    if trajectory_executor is not None:
        trajectory_executor.shutdown(wait=False)

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
            with io_lock:
                _append_jsonl(failed_trajectories_path, record)
            if not config.continue_on_error:
                raise

    with manifest_lock:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
