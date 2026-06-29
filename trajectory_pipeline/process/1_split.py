from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from prefix_format import eval_style_records, extract_messages, extract_steps, normalize_text


# =============================================================================
# 配置（硬编码，按需修改）
# =============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

# 输入：trajectory 目录列表（相对 ROOT_DIR）。同级 approved_cases 用来取 instruction 和 metadata。
INPUT_SOURCES = [
    # "results/mcp_strategy_2_20260416_120214/trajectories/trajectory_20260416_120214",
    # "results/mcp_strategy_2_20260416_143028/trajectories/trajectory_20260416_143028",
]

OUTPUT_ROOT = ROOT_DIR / "process" / "split"

# 与 eval_framework/run_eval.sh 的 MAX_INPUT_CHARS 保持一致；超长 prefix 先跳过。
MAX_PREFIX_CHARS = 20000

# None 表示为每条 trajectory 生成所有 step prefix，严格对齐 eval_framework.runner.replay。
MAX_PREFIXES_PER_TRAJECTORY: int | None = None

# 每路 source 最多读取的源 trajectory 数；None 表示全量。
SOURCE_LIMIT: int | None = None


# =============================================================================
# Source
# =============================================================================

@dataclass
class SourceSpec:
    trajectory_dir: Path
    approved_cases_dir: Path
    run_name: str


def load_sources() -> list[SourceSpec]:
    sources: list[SourceSpec] = []
    for path_text in INPUT_SOURCES:
        trajectory_dir = ROOT_DIR / path_text
        run_dir = trajectory_dir.parents[1]
        sources.append(
            SourceSpec(
                trajectory_dir=trajectory_dir,
                approved_cases_dir=run_dir / "approved_cases",
                run_name=run_dir.name,
            )
        )
    return sources


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_case_id(path: Path) -> int:
    matched = re.search(r"case_(\d+)_trajectory\.json$", path.name)
    if matched is None:
        raise ValueError(f"Invalid trajectory filename: {path.name}")
    return int(matched.group(1))


def collect_source_tasks(sources: list[SourceSpec]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for source in sources:
        if not source.trajectory_dir.exists():
            print(f"[warn] missing trajectory dir: {source.trajectory_dir}", flush=True)
            continue
        for trajectory_path in sorted(source.trajectory_dir.glob("case_*_trajectory.json")):
            case_id = extract_case_id(trajectory_path)
            case_path = source.approved_cases_dir / f"case_{case_id}.json"
            if not case_path.exists():
                print(f"[warn] missing case file: {case_path}", flush=True)
                continue
            tasks.append(
                {
                    "source": source,
                    "case_id": case_id,
                    "trajectory_path": trajectory_path,
                    "case_path": case_path,
                }
            )
            if SOURCE_LIMIT is not None and len(tasks) >= SOURCE_LIMIT:
                return tasks
    return tasks


def case_category(case_payload: dict[str, Any]) -> str:
    case = case_payload.get("case", {}) if isinstance(case_payload, dict) else {}
    metadata = case_payload.get("metadata", case.get("metadata", {}))
    if not isinstance(metadata, dict):
        metadata = {}
    return normalize_text(metadata.get("category")) or "unknown"


def within_prefix_budget(record: dict[str, Any]) -> bool:
    instruction = normalize_text(record.get("instruction"))
    prefix = normalize_text(record.get("trajectory_1"))
    return len(instruction) + len(prefix) <= MAX_PREFIX_CHARS


def make_output_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    output_dir = make_output_dir()
    splits_path = output_dir / "splits.json"

    sources = load_sources()
    source_tasks = collect_source_tasks(sources)
    print(f"sources={len(sources)} trajectories={len(source_tasks)} output_dir={output_dir}", flush=True)

    splits: list[dict[str, Any]] = []
    skipped_no_steps = 0
    skipped_too_long = 0
    prefixes_per_source: Counter[int] = Counter()

    for index, task in enumerate(source_tasks, start=1):
        source: SourceSpec = task["source"]
        case_payload = load_json(task["case_path"])
        trajectory_payload = load_json(task["trajectory_path"])

        steps = extract_steps(extract_messages(trajectory_payload))
        if not steps:
            skipped_no_steps += 1
            continue

        records = eval_style_records(
            steps=steps,
            source_id=f"{source.run_name}__case_{task['case_id']}",
            source_run=source.run_name,
            source_trajectory_file=str(task["trajectory_path"].relative_to(ROOT_DIR)),
            case_id=task["case_id"],
            category=case_category(case_payload),
        )
        if MAX_PREFIXES_PER_TRAJECTORY is not None:
            records = records[:MAX_PREFIXES_PER_TRAJECTORY]

        kept = 0
        for record in records:
            if not within_prefix_budget(record):
                skipped_too_long += 1
                continue
            splits.append(record)
            kept += 1

        prefixes_per_source[kept] += 1
        if index % 50 == 0 or index == len(source_tasks):
            splits_path.write_text(json.dumps(splits, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[{index}/{len(source_tasks)}] splits={len(splits)}", flush=True)

    splits_path.write_text(json.dumps(splits, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "generated_at": datetime.now().isoformat(),
        "input_sources": INPUT_SOURCES,
        "max_prefix_chars": MAX_PREFIX_CHARS,
        "max_prefixes_per_trajectory": MAX_PREFIXES_PER_TRAJECTORY,
        "source_trajectories": len(source_tasks),
        "skipped_no_steps": skipped_no_steps,
        "skipped_too_long": skipped_too_long,
        "total_splits": len(splits),
        "prefixes_per_source_distribution": dict(prefixes_per_source),
        "format": "eval_style_prefix",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 60)
    print(f"source_trajectories: {len(source_tasks)}")
    print(f"skipped_no_steps: {skipped_no_steps}")
    print(f"skipped_too_long: {skipped_too_long}")
    print(f"total_splits: {len(splits)}")
    print(f"output: {splits_path}")


if __name__ == "__main__":
    main()
