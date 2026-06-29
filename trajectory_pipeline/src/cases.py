from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Dict

from config import build_config, validate_config
from modules.run import run


ROOT_DIR = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT_DIR / "compiled_prompts"
RESULTS_DIR = ROOT_DIR / "results"
RUN_NAME = "generated_original_flow"
CATEGORY_DISPLAY_NAMES: Dict[str, str] = {
    "copyright": "Copyright",
    "fraud": "Fraud",
    "harassment": "Harassment",
    "sexual": "Sexual",
    "cybercrime": "Cybercrime",
    "disinformation": "Disinformation",
    "drugs": "Drugs",
    "hate": "Hate",
    "failure_strategy": "Failure Strategy",
    "direct": "Direct Attack",
    "ignore_previous": "Ignore Previous Attack",
    "system_message": "System Message Attack",
    "injecagent": "InjecAgent Attack",
    "important_instructions": "Important Instructions Attack",
    "important_instructions_ablation": "Important Instructions Ablation",
    "tool_knowledge": "Tool Knowledge Attack",
    "manual": "Manual Attack",
    "dos": "DoS Attack",
    "swearwords_dos": "Swearwords DoS Attack",
    "captcha_dos": "CAPTCHA DoS Attack",
    "offensive_email_dos": "Offensive Email DoS Attack",
    "felony_dos": "Felony DoS Attack",
    # strategy_8_agentdojo_suite categories
    "benign": "AgentDojo Benign Baseline",
    # strategy_10_asb_methods categories
    "clean": "ASB Clean (no attack)",
    "DPI": "ASB Direct Prompt Injection",
    "MP": "ASB Memory Poisoning",
    "OPI": "ASB Observation Prompt Injection",
}
CATEGORY_COUNTS: Dict[str, int] = {
    "copyright": 7,
    "fraud": 7,
    "harassment": 7,
    "sexual": 7,
    "cybercrime": 6,
    "disinformation": 6,
    "drugs": 6,
    "hate": 6,
}
AUTO_APPROVE = True
MAX_ITERATIONS = 3
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000/v1")
API_KEY = os.environ.get("API_KEY", "EMPTY")
MODEL = os.environ.get("MODEL", "kimi-k2.5")
TEMPERATURE = 0.7
CASE_RETRY_LIMIT = 2
RUN_TRAJECTORY = True
TRACE_RUN_NAME = "trajectory"
TRAJECTORY_BASE_URL = os.environ.get("TRAJECTORY_BASE_URL", BASE_URL)
TRAJECTORY_API_KEY = os.environ.get("TRAJECTORY_API_KEY", API_KEY)
TRAJECTORY_MODEL = os.environ.get("TRAJECTORY_MODEL", MODEL)
TRAJECTORY_TEMPERATURE = 0.0
TRAJECTORY_STYLE = "auto"
TOOL_EXECUTOR_BASE_URL = os.environ.get("TOOL_EXECUTOR_BASE_URL", TRAJECTORY_BASE_URL)
TOOL_EXECUTOR_API_KEY = os.environ.get("TOOL_EXECUTOR_API_KEY", TRAJECTORY_API_KEY)
TOOL_EXECUTOR_MODEL = os.environ.get("TOOL_EXECUTOR_MODEL", TRAJECTORY_MODEL)
TOOL_EXECUTOR_TEMPERATURE = 0.0
TRAJECTORY_MAX_TURNS = 25
TRAJECTORY_WORKERS = 4
CASE_WORKERS = 4
CONTINUE_ON_ERROR = True
CASE_SCHEDULE = "sequential"


def _parse_count(value: str) -> tuple[str, int]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"Invalid count '{value}'. Expected format: category=count")
    category, count_text = value.split("=", 1)
    category = category.strip()
    if not category:
        raise argparse.ArgumentTypeError(f"Invalid count '{value}'. Category cannot be empty")
    try:
        count = int(count_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid count '{value}'. Count must be an integer") from exc
    if count <= 0:
        raise argparse.ArgumentTypeError(f"Invalid count '{value}'. Count must be > 0")
    return category, count


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate LPS-style cases from compiled prompts.")
    parser.add_argument("--prompt-dir", type=Path, default=PROMPT_DIR)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-name", default=RUN_NAME)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--count", action="append", type=_parse_count, default=[])
    parser.add_argument("--auto-approve", action=argparse.BooleanOptionalAction, default=AUTO_APPROVE)
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--api-key", default=API_KEY)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--temperature", type=float, default=TEMPERATURE)
    parser.add_argument("--case-retry-limit", type=int, default=CASE_RETRY_LIMIT)
    parser.add_argument("--run-trajectory", action=argparse.BooleanOptionalAction, default=RUN_TRAJECTORY)
    parser.add_argument("--trace-run-name", default=TRACE_RUN_NAME)
    parser.add_argument("--trajectory-base-url", default=TRAJECTORY_BASE_URL)
    parser.add_argument("--trajectory-api-key", default=TRAJECTORY_API_KEY)
    parser.add_argument("--trajectory-model", default=TRAJECTORY_MODEL)
    parser.add_argument("--trajectory-temperature", type=float, default=TRAJECTORY_TEMPERATURE)
    parser.add_argument("--trajectory-style", choices=("auto", "openai", "xml", "react"), default=TRAJECTORY_STYLE)
    parser.add_argument("--tool-executor-base-url", default=TOOL_EXECUTOR_BASE_URL)
    parser.add_argument("--tool-executor-api-key", default=TOOL_EXECUTOR_API_KEY)
    parser.add_argument("--tool-executor-model", default=TOOL_EXECUTOR_MODEL)
    parser.add_argument("--tool-executor-temperature", type=float, default=TOOL_EXECUTOR_TEMPERATURE)
    parser.add_argument("--trajectory-max-turns", type=int, default=TRAJECTORY_MAX_TURNS)
    parser.add_argument("--trajectory-workers", type=int, default=TRAJECTORY_WORKERS)
    parser.add_argument("--case-workers", type=int, default=CASE_WORKERS)
    parser.add_argument("--continue-on-error", action=argparse.BooleanOptionalAction, default=CONTINUE_ON_ERROR)
    parser.add_argument("--case-schedule", choices=("sequential", "proportional"), default=CASE_SCHEDULE)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    category_counts = dict(CATEGORY_COUNTS)
    if args.count:
        category_counts = dict(args.count)
    output_dir = args.output_dir or (args.results_dir / args.run_name)

    config = build_config(
        prompt_dir=args.prompt_dir,
        output_dir=output_dir,
        category_counts=category_counts,
        category_display_names=CATEGORY_DISPLAY_NAMES,
        resume=args.resume,
        auto_approve=args.auto_approve,
        max_iterations=args.max_iterations,
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        temperature=args.temperature,
        case_retry_limit=args.case_retry_limit,
        run_trajectory=args.run_trajectory,
        trace_run_name=args.trace_run_name,
        trajectory_base_url=args.trajectory_base_url,
        trajectory_api_key=args.trajectory_api_key,
        trajectory_model=args.trajectory_model,
        trajectory_temperature=args.trajectory_temperature,
        trajectory_style=args.trajectory_style,
        tool_executor_base_url=args.tool_executor_base_url,
        tool_executor_api_key=args.tool_executor_api_key,
        tool_executor_model=args.tool_executor_model,
        tool_executor_temperature=args.tool_executor_temperature,
        trajectory_max_turns=args.trajectory_max_turns,
        trajectory_workers=args.trajectory_workers,
        case_workers=args.case_workers,
        continue_on_error=args.continue_on_error,
        case_schedule=args.case_schedule,
    )
    validate_config(config)
    run(config)


if __name__ == "__main__":
    main()
