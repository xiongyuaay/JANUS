from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval_framework.guards.prompts import TRUTH_FUTURE_SUMMARY_TEMPLATE
from eval_framework.guards.vllm_guard import (
    MAX_TOTAL_INPUT_CHARS,
    VLLMGuard,
    _default_format_history,
    _extract_instruction,
    _format_observed,
    fit_messages_to_char_budget,
    make_future_summary_cache_key,
)
from eval_framework.loaders import available as loaders_available
from eval_framework.loaders import get_loader
from eval_framework.schema import Split, Step, Trajectory

logger = logging.getLogger("cache_future_summaries")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand_env_value(value):
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            return os.environ.get(name, default or "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_value(item) for key, item in value.items()}
    return value

# ============================= CONFIG =======================================
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "../eval_framework_online/STATIC_TRACE"))
BENCHMARKS = "agentdojo,agentharm,asb,agent_safetybench,agentlab,lps_bench"
MODELS = "react_base"
SPLITS = "benign,adversarial"
LIMIT = None
GUARD_CONFIG = Path("configs/vllm_example.json")
OUT = Path("runs/future_summary_cache.json")
CONCURRENCY = 32
MAX_INPUT_CHARS = 20000
MAX_TOKENS = 256
TEMPERATURE = 0.0
LOG_LEVEL = "INFO"
# ============================== END =========================================


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scripts/cache_future_summaries.py")
    p.add_argument("--data-root", type=Path, default=DATA_ROOT)
    p.add_argument("--benchmarks", default=BENCHMARKS)
    p.add_argument("--models", default=MODELS)
    p.add_argument("--splits", default=SPLITS)
    p.add_argument("--limit", type=int, default=LIMIT)
    p.add_argument("--guard-config", type=Path, default=GUARD_CONFIG)
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--concurrency", type=int, default=CONCURRENCY)
    p.add_argument("--max-input-chars", type=int, default=MAX_INPUT_CHARS)
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    p.add_argument("--temperature", type=float, default=TEMPERATURE)
    p.add_argument(
        "--log-level",
        default=LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def _resolve_benchmarks(arg: str) -> list[str]:
    if arg == "all" or not arg.strip():
        return loaders_available()
    names = [n.strip() for n in arg.split(",") if n.strip()]
    valid = set(loaders_available())
    bad = [n for n in names if n not in valid]
    if bad:
        raise SystemExit(f"unknown benchmark(s) {bad}; available: {sorted(valid)}")
    return names


def _resolve_splits(arg: str) -> list[Split]:
    out = [s.strip() for s in arg.split(",") if s.strip()]
    bad = [s for s in out if s not in ("benign", "adversarial")]
    if bad:
        raise SystemExit(f"unknown split(s) {bad}")
    return out  # type: ignore[return-value]


def _model_filter(arg: Optional[str]) -> Optional[list[str]]:
    if not arg:
        return None
    return [m.strip() for m in arg.split(",") if m.strip()]


def _load_guard(args: argparse.Namespace) -> VLLMGuard:
    config = _expand_env_value(json.loads(args.guard_config.read_text(encoding="utf-8")))
    max_input_chars = args.max_input_chars
    if max_input_chars is None:
        max_input_chars = int(config.get("max_input_chars", MAX_TOTAL_INPUT_CHARS))
    return VLLMGuard(
        base_url=config["base_url"],
        model_name=config["model_name"],
        api_key=config.get("api_key"),
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        timeout=float(config.get("timeout", 60.0)),
        extra_body=config.get("extra_body"),
        max_input_chars=max_input_chars,
    )


def _future_summary_prompt(history: tuple[Step, ...], future: tuple[Step, ...]) -> str:
    future_text = _default_format_history(future) if future else "(no future steps)"
    return (
        TRUTH_FUTURE_SUMMARY_TEMPLATE
        .replace("{instruction}", _extract_instruction(history))
        .replace("{tpast}", _format_observed(history))
        .replace("{tfuture}", future_text)
    )


def _summarize_step(
    guard: VLLMGuard,
    trajectory: Trajectory,
    step_pos: int,
) -> tuple[str, str]:
    step = trajectory.steps[step_pos]
    key = make_future_summary_cache_key(
        trajectory.benchmark,
        trajectory.model,
        trajectory.split,
        trajectory.id,
        step.index,
    )
    future = trajectory.steps[step_pos + 1 :]
    if not future:
        return key, "(no future steps)"
    prompt = _future_summary_prompt(trajectory.steps[: step_pos + 1], future)
    messages = [{"role": "user", "content": prompt}]
    messages = fit_messages_to_char_budget(messages, budget=guard.max_input_chars)
    return key, guard._request(messages).strip()


def _load_trajectories(args: argparse.Namespace) -> list[Trajectory]:
    benchmarks = _resolve_benchmarks(args.benchmarks)
    splits = _resolve_splits(args.splits)
    wanted_models = _model_filter(args.models)
    trajectories: list[Trajectory] = []
    for bench in benchmarks:
        loader = get_loader(bench)
        models = wanted_models if loader.uses_model_filter(args.data_root) else None
        trajectories.extend(
            loader.iter_trajectories(
                args.data_root,
                splits=splits,
                models=models,
                limit=args.limit,
            )
        )
    return trajectories


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    guard = _load_guard(args)
    trajectories = _load_trajectories(args)
    total_steps = sum(len(t.steps) for t in trajectories)
    logger.info("loaded %d trajectories, %d steps", len(trajectories), total_steps)

    jobs = [(traj, step_pos) for traj in trajectories for step_pos in range(len(traj.steps))]
    records: dict[str, str] = {}
    if args.concurrency > 1:
        guard._get_client()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(_summarize_step, guard, traj, step_pos): (traj, step_pos)
                for traj, step_pos in jobs
            }
            for i, fut in enumerate(as_completed(futures), 1):
                key, summary = fut.result()
                records[key] = summary
                if i % 100 == 0:
                    logger.info("summarized %d/%d steps", i, len(jobs))
    else:
        for i, (traj, step_pos) in enumerate(jobs, 1):
            key, summary = _summarize_step(guard, traj, step_pos)
            records[key] = summary
            if i % 100 == 0:
                logger.info("summarized %d/%d steps", i, len(jobs))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "format": "janus_future_summary_cache_v1",
                "data_root": str(args.data_root),
                "benchmarks": _resolve_benchmarks(args.benchmarks),
                "splits": _resolve_splits(args.splits),
                "models_filter": _model_filter(args.models),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("wrote %d summaries to %s", len(records), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
