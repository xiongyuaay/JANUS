from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import LLMConfig
from modules.trajectory import TrajectoryFlow


ROOT_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT_DIR / "results"
RUN_NAME = "generated_original_flow"
INPUT_CASES_DIR = RESULTS_DIR / RUN_NAME / "approved_cases"
OUTPUT_DIR = RESULTS_DIR / RUN_NAME / "trajectories"
CASE_FILE_FILTER: Optional[str] = None
MAX_CASES: Optional[int] = None

TRAJECTORY_BASE_URL = os.environ.get("TRAJECTORY_BASE_URL", os.environ.get("BASE_URL", "http://localhost:8000/v1"))
TRAJECTORY_API_KEY = os.environ.get("TRAJECTORY_API_KEY", os.environ.get("API_KEY", "EMPTY"))
TRAJECTORY_MODEL = os.environ.get("TRAJECTORY_MODEL", os.environ.get("MODEL", "qwen-3.5-122B-uncensored-stxt"))
TRAJECTORY_TEMPERATURE = 0.0

TOOL_EXECUTOR_BASE_URL = TRAJECTORY_BASE_URL
TOOL_EXECUTOR_API_KEY = TRAJECTORY_API_KEY
TOOL_EXECUTOR_MODEL = TRAJECTORY_MODEL
TOOL_EXECUTOR_TEMPERATURE = 0.0

MAX_TURNS = 12
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


def load_case_files(input_dir: Path, case_filter: Optional[str], max_cases: Optional[int]) -> List[Path]:
    paths = sorted(input_dir.glob("case_*.json"))
    if case_filter:
        paths = [path for path in paths if case_filter in path.name]
    if max_cases is not None:
        paths = paths[:max_cases]
    return paths


def load_case(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def create_failed_case_record(case_path: Path, error: Exception) -> Dict[str, Any]:
    """Create a compact record for one failed trajectory case."""

    return {
        "case_file": case_path.name,
        "error_type": type(error).__name__,
        "error": str(error),
    }


def append_failed_case(output_dir: Path, case_path: Path, error: Exception) -> None:
    """Append one failed case record for later inspection."""

    failure_path = output_dir / "failed_cases.jsonl"
    with failure_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(create_failed_case_record(case_path, error), ensure_ascii=False) + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate trajectories from approved cases.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-name", default=RUN_NAME)
    parser.add_argument("--input-cases-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--case-filter", default=CASE_FILE_FILTER)
    parser.add_argument("--max-cases", type=int, default=MAX_CASES)
    parser.add_argument("--trajectory-base-url", default=TRAJECTORY_BASE_URL)
    parser.add_argument("--trajectory-api-key", default=TRAJECTORY_API_KEY)
    parser.add_argument("--trajectory-model", default=TRAJECTORY_MODEL)
    parser.add_argument("--trajectory-temperature", type=float, default=TRAJECTORY_TEMPERATURE)
    parser.add_argument("--tool-executor-base-url", default=TOOL_EXECUTOR_BASE_URL)
    parser.add_argument("--tool-executor-api-key", default=TOOL_EXECUTOR_API_KEY)
    parser.add_argument("--tool-executor-model", default=TOOL_EXECUTOR_MODEL)
    parser.add_argument("--tool-executor-temperature", type=float, default=TOOL_EXECUTOR_TEMPERATURE)
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--retry-backoff-seconds", type=float, default=RETRY_BACKOFF_SECONDS)
    parser.add_argument("--continue-on-error", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _build_llm_config(base_url: str, api_key: str, model: str, temperature: float, max_retries: int, retry_backoff_seconds: float) -> LLMConfig:
    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )


def main() -> None:
    args = _build_parser().parse_args()

    run_dir = args.results_dir / args.run_name
    input_cases_dir = args.input_cases_dir or (run_dir / "approved_cases")
    output_dir = args.output_dir or (run_dir / "trajectories")
    output_dir.mkdir(parents=True, exist_ok=True)

    trajectory_config = _build_llm_config(
        args.trajectory_base_url,
        args.trajectory_api_key,
        args.trajectory_model,
        args.trajectory_temperature,
        args.max_retries,
        args.retry_backoff_seconds,
    )
    tool_executor_config = _build_llm_config(
        args.tool_executor_base_url,
        args.tool_executor_api_key,
        args.tool_executor_model,
        args.tool_executor_temperature,
        args.max_retries,
        args.retry_backoff_seconds,
    )
    flow = TrajectoryFlow(trajectory_config, tool_executor_config)

    skip_existing = args.resume or args.skip_existing
    case_paths = load_case_files(input_cases_dir, args.case_filter, None)
    if not skip_existing and args.max_cases is not None:
        case_paths = case_paths[: args.max_cases]

    existing_outputs = sorted(output_dir.glob("case_*_trajectory.json")) if skip_existing else []
    remaining = None if args.max_cases is None else max(0, args.max_cases - len(existing_outputs))
    total_cases = len(case_paths)
    completed_cases = 0

    for case_index, case_path in enumerate(case_paths, start=1):
        if remaining == 0:
            break
        output_path = output_dir / f"{case_path.stem}_trajectory.json"
        if skip_existing and output_path.exists():
            print(f"[case {case_index}/{total_cases}] Skipped existing trajectory: {output_path}", flush=True)
            continue

        print(f"[case {case_index}/{total_cases}] Starting trajectory for {case_path.name}", flush=True)
        case_payload = load_case(case_path)
        try:
            trajectory = flow.run(case_payload, max_turns=args.max_turns)
            output_path.write_text(json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
            completed_cases += 1
            print(
                f"[case {case_index}/{total_cases}] Saved trajectory to {output_path} "
                f"(completed={completed_cases})",
                flush=True,
            )
        except Exception as exc:
            append_failed_case(output_dir, case_path, exc)
            print(f"[case {case_index}/{total_cases}] Failed trajectory for {case_path.name}: {exc}", flush=True)
            if not args.continue_on_error:
                raise
        if remaining is not None:
            remaining -= 1


if __name__ == "__main__":
    main()
