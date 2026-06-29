"""CLI entry point.

Examples
--------

    # Baseline numbers (no guard, no mode): per-benchmark utility / ASR.
    python -m eval_framework.cli \
        --data-root /mnt/.../eval_data \
        --benchmarks agentdojo \
        --models gpt-4o-2024-05-13 \
        --out runs/baseline.json

    # Full evaluation with a guard in lenient mode.
    python -m eval_framework.cli \
        --data-root /mnt/.../eval_data \
        --benchmarks all \
        --models gpt-4o-2024-05-13,claude-3-5-sonnet-20241022 \
        --guard vllm --guard-config configs/vllm_example.json \
        --mode lenient \
        --out runs/report.json
"""
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

from . import aggregator, metrics, runner
from .aggregator import TrajectoryResult
from .guards import available as guards_available
from .guards import build_guard
from .loaders import available as loaders_available
from .loaders import get_loader

logger = logging.getLogger("eval_framework")

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


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="eval_framework.cli")
    p.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Root directory containing <benchmark>/{benign,adversarial}/... .",
    )
    p.add_argument(
        "--benchmarks",
        default="all",
        help="Comma-separated benchmark names, or 'all'. "
        f"Available: {','.join(loaders_available())}",
    )
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model filter (applied per benchmark if the "
        "benchmark has a per-model layout; ignored otherwise). If omitted, "
        "all models on disk are included.",
    )
    p.add_argument(
        "--guard",
        default=None,
        choices=guards_available(),
        help="Guard implementation. If omitted, only baseline utility / ASR "
        "(without guard) are reported.",
    )
    p.add_argument(
        "--guard-config",
        type=Path,
        default=None,
        help="Optional JSON file with guard-specific config.",
    )
    p.add_argument(
        "--mode",
        choices=["lenient", "strict"],
        default="lenient",
        help="Label→decision mapping mode. lenient: only 'unsafe' blocks; "
        "strict: 'unsafe' and 'potential_unsafe' both block. Only meaningful "
        "when --guard is provided.",
    )
    p.add_argument(
        "--splits",
        default="benign,adversarial",
        help="Comma-separated splits to load.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on trajectories loaded per benchmark.",
    )
    p.add_argument(
        "--stop-on-first-flag",
        action="store_true",
        help="Truncate step-verdict list once a step is flagged.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of trajectories to replay in parallel (thread pool). "
        "1 = sequential (default). Only effective when --guard is set.",
    )
    p.add_argument(
        "--benchmark-concurrency",
        type=int,
        default=1,
        help="Number of benchmark worker groups. With --guard and multiple benchmarks, "
        "the total trajectory worker budget is concurrency * benchmark_concurrency "
        "and is shared dynamically across benchmarks.",
    )
    p.add_argument(
        "--max-input-chars",
        type=int,
        default=None,
        help="Unified cap on total guard input length in characters. Applies "
        "across all message contents and fully replaces any per-config char "
        "limits. If omitted, the guard's built-in default is used.",
    )
    p.add_argument(
        "--vllm-predict-future-summary",
        action="store_true",
        help="For --guard vllm, generate a predicted future summary before each judge call.",
    )
    p.add_argument(
        "--vllm-truth-future-summary",
        action="store_true",
        help="For --guard vllm, read ground-truth future summaries from a cache before judging.",
    )
    p.add_argument(
        "--vllm-future-summary-cache",
        type=Path,
        default=None,
        help="Future summary cache path used with --vllm-truth-future-summary.",
    )
    p.add_argument(
        "--vllm-random-future-summary",
        action="store_true",
        help="For --guard vllm, use a deterministic random mismatched cached future summary before judging.",
    )
    p.add_argument(
        "--judge-prefix-ratio",
        type=float,
        choices=[0.25, 0.5, 0.75],
        default=None,
        help="Cap judge-visible trajectory history to the first ratio of the full trajectory.",
    )
    p.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output JSON path (full details + metrics).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


def _load_guard_config(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    return _expand_env_value(json.loads(path.read_text(encoding="utf-8")))


def _resolve_benchmarks(arg: str) -> list[str]:
    arg = arg.strip()
    if arg == "all" or not arg:
        return loaders_available()
    names = [n.strip() for n in arg.split(",") if n.strip()]
    valid = set(loaders_available())
    bad = [n for n in names if n not in valid]
    if bad:
        raise SystemExit(f"unknown benchmark(s) {bad}; available: {sorted(valid)}")
    return names


def _resolve_splits(arg: str) -> list[str]:
    out = [s.strip() for s in arg.split(",") if s.strip()]
    bad = [s for s in out if s not in ("benign", "adversarial")]
    if bad:
        raise SystemExit(f"unknown split(s) {bad}")
    return out


def _group_results_by_model(
    results: list[TrajectoryResult],
) -> dict[Optional[str], list[TrajectoryResult]]:
    buckets: dict[Optional[str], list[TrajectoryResult]] = {}
    for r in results:
        buckets.setdefault(r.model, []).append(r)
    return buckets


# ---------- table rendering ------------------------------------------------


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _render_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "  "
    out = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append(sep.join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(out)


def _render_metrics_table(
    buckets: list[metrics.BucketMetrics], guard_applied: bool
) -> str:
    headers = [
        "benchmark",
        "model",
        "mode",
        "N_ben_all",
        "N_ben_safe",
        "base_util",
        "final_util",
        "FPR",
        "N_adv",
        "base_asr",
        "final_asr",
        "defend",
    ]
    if not guard_applied:
        keep = {
            "benchmark",
            "model",
            "N_ben_all",
            "N_ben_safe",
            "base_util",
            "N_adv",
            "base_asr",
        }
        headers = [h for h in headers if h in keep]

    rows: list[list[str]] = []
    for b in buckets:
        full = {
            "benchmark": b.benchmark,
            "model": b.model or "-",
            "mode": b.mode or "-",
            "N_ben_all": _fmt(b.benign.n),
            "N_ben_safe": _fmt(b.benign.n_with_labels),
            "base_util": _fmt(b.benign.base_utility),
            "final_util": _fmt(b.benign.final_utility),
            "FPR": _fmt(b.benign.fpr),
            "N_adv": _fmt(b.adversarial.n_with_labels),
            "base_asr": _fmt(b.adversarial.base_asr),
            "final_asr": _fmt(b.adversarial.final_asr),
            "defend": _fmt(b.adversarial.defend_rate),
        }
        rows.append([full[h] for h in headers])
    return _render_table(rows, headers)


# ---------- main -----------------------------------------------------------


def _process_bucket(
    benchmark: str,
    model: Optional[str],
    trajectories_with_verdicts,
    mode: str,
    guard_applied: bool,
) -> tuple[metrics.BucketMetrics, list[dict]]:
    results: list[TrajectoryResult] = []
    details: list[dict] = []
    for traj, verdicts in trajectories_with_verdicts:
        res = aggregator.aggregate(traj, verdicts, mode=mode)  # type: ignore[arg-type]
        results.append(res)
        details.append(
            {
                "benchmark": res.benchmark,
                "trajectory_id": res.trajectory_id,
                "split": res.split,
                "model": res.model,
                "utility": res.utility,
                "security": res.security,
                "flagged": res.flagged,
                "first_block_step": res.first_block_step,
                "num_steps": res.num_steps,
                "step_labels": res.step_labels,
                "step_reasons": [v.reason for v in res.step_verdicts],
            }
        )
    m = metrics.compute(
        benchmark, model, results, mode=mode, guard_applied=guard_applied
    )
    return m, details


def _load_benchmark_groups(
    bench: str,
    *,
    args: argparse.Namespace,
    splits: list[str],
    model_filter: Optional[list[str]],
) -> list[tuple[str, Optional[str], list]]:
    loader = get_loader(bench)
    has_model_dim = loader.uses_model_filter(args.data_root)
    effective_filter = model_filter if has_model_dim else None
    trajs = list(
        loader.iter_trajectories(
            args.data_root,
            splits=splits,  # type: ignore[arg-type]
            models=effective_filter,
            limit=args.limit,
        )
    )
    if not trajs:
        logger.info("%s: no trajectories found, skipping", bench)
        return []

    if has_model_dim:
        by_model = {}
        for t in trajs:
            by_model.setdefault(t.model, []).append(t)
    else:
        by_model = {None: trajs}
    return [(bench, model_name, traj_list) for model_name, traj_list in by_model.items()]


def _run_global_guard_pool(
    groups: list[tuple[str, Optional[str], list]],
    *,
    args: argparse.Namespace,
    guard,
    tqdm,
) -> tuple[list[metrics.BucketMetrics], list[dict]]:
    total_workers = max(1, args.concurrency * args.benchmark_concurrency)
    total_trajs = sum(len(traj_list) for _, _, traj_list in groups)
    logger.info("global_trajectory_concurrency=%d", total_workers)

    if hasattr(guard, "_get_client"):
        guard._get_client()  # type: ignore[attr-defined]

    verdicts_by_group: list[dict[str, list]] = [dict() for _ in groups]
    with ThreadPoolExecutor(max_workers=total_workers) as pool:
        futures = {}
        for group_index, (_, _, traj_list) in enumerate(groups):
            if hasattr(guard, "validate_trajectories"):
                guard.validate_trajectories(traj_list)  # type: ignore[attr-defined]
            for traj in traj_list:
                future = pool.submit(
                    runner.replay,
                    traj,
                    guard,
                    stop_on_first_flag=args.stop_on_first_flag,
                    judge_prefix_ratio=args.judge_prefix_ratio,
                )
                futures[future] = (group_index, traj)

        completed = tqdm(
            as_completed(futures),
            total=total_trajs,
            desc="trajectories",
            unit="traj",
            dynamic_ncols=True,
            position=0,
            leave=True,
        )
        for fut in completed:
            group_index, traj = futures[fut]
            verdicts = fut.result()
            verdicts_by_group[group_index][traj.id] = verdicts
            flagged = any(v.flagged for v in verdicts)
            if hasattr(completed, "set_postfix"):
                completed.set_postfix(
                    benchmark=traj.benchmark,
                    steps=len(verdicts),
                    flagged=int(flagged),
                )

    buckets: list[metrics.BucketMetrics] = []
    details_out: list[dict] = []
    for group_index, (bench, model_name, traj_list) in enumerate(groups):
        pairs = [(t, verdicts_by_group[group_index].get(t.id, [])) for t in traj_list]
        bucket_m, details = _process_bucket(
            bench,
            model_name,
            pairs,
            mode=args.mode,
            guard_applied=True,
        )
        buckets.append(bucket_m)
        details_out.extend(details)
        logger.info(
            "%s/%s: benign_safe=%d/%d adv=%d base_util=%s base_asr=%s",
            bench,
            model_name or "-",
            bucket_m.benign.n_with_labels,
            bucket_m.benign.n,
            bucket_m.adversarial.n_with_labels,
            _fmt(bucket_m.benign.base_utility),
            _fmt(bucket_m.adversarial.base_asr),
        )
    return buckets, details_out


def _process_benchmark(
    bench: str,
    *,
    args: argparse.Namespace,
    splits: list[str],
    model_filter: Optional[list[str]],
    guard,
    tqdm,
    show_progress: bool,
    progress_position: int = 0,
) -> tuple[list[metrics.BucketMetrics], list[dict]]:
    buckets: list[metrics.BucketMetrics] = []
    details_out: list[dict] = []
    for _, model_name, traj_list in _load_benchmark_groups(
        bench,
        args=args,
        splits=splits,
        model_filter=model_filter,
    ):
        desc = f"{bench}/{model_name or '-'}"
        parallel = guard is not None and args.concurrency > 1
        if guard is not None and hasattr(guard, "validate_trajectories"):
            guard.validate_trajectories(traj_list)  # type: ignore[attr-defined]

        if parallel:
            # Prewarm the guard's HTTP client so the worker threads don't
            # race on lazy initialisation.
            if hasattr(guard, "_get_client"):
                try:
                    guard._get_client()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 — best-effort
                    logger.debug("guard client prewarm failed", exc_info=True)

            verdicts_by_id: dict[str, list] = {}
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {
                    pool.submit(
                        runner.replay,
                        t,
                        guard,
                        stop_on_first_flag=args.stop_on_first_flag,
                        judge_prefix_ratio=args.judge_prefix_ratio,
                    ): t
                    for t in traj_list
                }
                completed = as_completed(futures)
                if show_progress:
                    completed = tqdm(
                        completed,
                        total=len(futures),
                        desc=desc,
                        unit="traj",
                        dynamic_ncols=True,
                        position=progress_position,
                        leave=True,
                    )
                for fut in completed:
                    traj = futures[fut]
                    try:
                        verdicts = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("replay failed for %s", traj.id)
                        verdicts = []
                    verdicts_by_id[traj.id] = verdicts
                    flagged = any(v.flagged for v in verdicts)
                    if show_progress and hasattr(completed, "set_postfix"):
                        completed.set_postfix(steps=len(traj.steps), flagged=int(flagged))
            pairs = [(t, verdicts_by_id.get(t.id, [])) for t in traj_list]
        else:
            pairs = []
            iterator = traj_list
            if show_progress:
                iterator = tqdm(
                    traj_list,
                    desc=desc,
                    unit="traj",
                    dynamic_ncols=True,
                    position=progress_position,
                    leave=True,
                )
            for traj in iterator:
                if guard is not None:
                    verdicts = runner.replay(
                        traj,
                        guard,
                        stop_on_first_flag=args.stop_on_first_flag,
                        judge_prefix_ratio=args.judge_prefix_ratio,
                    )
                    flagged = any(v.flagged for v in verdicts)
                    if show_progress and hasattr(iterator, "set_postfix"):
                        iterator.set_postfix(steps=len(traj.steps), flagged=int(flagged))
                else:
                    verdicts = []
                pairs.append((traj, verdicts))

        bucket_m, details = _process_bucket(
            bench,
            model_name,
            pairs,
            mode=args.mode,
            guard_applied=guard is not None,
        )
        buckets.append(bucket_m)
        details_out.extend(details)

        logger.info(
            "%s/%s: benign_safe=%d/%d adv=%d base_util=%s base_asr=%s",
            bench,
            model_name or "-",
            bucket_m.benign.n_with_labels,
            bucket_m.benign.n,
            bucket_m.adversarial.n_with_labels,
            _fmt(bucket_m.benign.base_utility),
            _fmt(bucket_m.adversarial.base_asr),
        )
    return buckets, details_out


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover
        def tqdm(x, **_):  # type: ignore[no-redef]
            return x

    benchmarks = _resolve_benchmarks(args.benchmarks)
    splits = _resolve_splits(args.splits)
    model_filter: Optional[list[str]] = (
        [m.strip() for m in args.models.split(",") if m.strip()] if args.models else None
    )

    guard = None
    guard_config: dict = {}
    if args.guard:
        if args.guard != "vllm" and (
            args.vllm_predict_future_summary
            or args.vllm_truth_future_summary
            or args.vllm_future_summary_cache is not None
            or args.vllm_random_future_summary
        ):
            raise SystemExit("vLLM future-summary switches can only be used with --guard vllm")
        if args.vllm_predict_future_summary and (
            args.vllm_truth_future_summary or args.vllm_random_future_summary
        ):
            raise SystemExit(
                "--vllm-predict-future-summary cannot be combined with truth/random future summaries"
            )
        guard_config = _load_guard_config(args.guard_config)
        if args.max_input_chars is not None:
            guard_config["max_input_chars"] = args.max_input_chars
        if args.vllm_predict_future_summary:
            guard_config["predict_future_summary"] = True
        if args.vllm_truth_future_summary or args.vllm_random_future_summary:
            guard_config["truth_future_summary"] = True
            if args.vllm_future_summary_cache is None:
                raise SystemExit(
                    "--vllm-future-summary-cache is required with truth/random future summaries"
                )
            guard_config["future_summary_cache"] = str(args.vllm_future_summary_cache)
        if args.vllm_random_future_summary:
            guard_config["future_summary_mismatch"] = True
        guard = build_guard(args.guard, guard_config)
        logger.info("guard=%s", guard.name)

    all_buckets: list[metrics.BucketMetrics] = []
    all_details: list[dict] = []
    benchmark_parallel = args.benchmark_concurrency > 1 and len(benchmarks) > 1
    if benchmark_parallel and guard is not None:
        groups = []
        for bench in benchmarks:
            groups.extend(
                _load_benchmark_groups(
                    bench,
                    args=args,
                    splits=splits,
                    model_filter=model_filter,
                )
            )
        bucket_items, detail_items = _run_global_guard_pool(
            groups,
            args=args,
            guard=guard,
            tqdm=tqdm,
        )
        all_buckets.extend(bucket_items)
        all_details.extend(detail_items)
    else:
        for bench in benchmarks:
            bucket_items, detail_items = _process_benchmark(
                bench,
                args=args,
                splits=splits,
                model_filter=model_filter,
                guard=guard,
                tqdm=tqdm,
                show_progress=True,
                progress_position=0,
            )
            all_buckets.extend(bucket_items)
            all_details.extend(detail_items)

    table = _render_metrics_table(all_buckets, guard_applied=guard is not None)
    print(table)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "data_root": str(args.data_root),
                "benchmarks": benchmarks,
                "splits": splits,
                "models_filter": model_filter,
                "benchmark_concurrency": args.benchmark_concurrency,
                "trajectory_concurrency": (
                    args.concurrency * args.benchmark_concurrency
                    if benchmark_parallel and guard is not None
                    else args.concurrency
                ),
                "judge_prefix_ratio": args.judge_prefix_ratio,
                "guard": args.guard,
                "guard_config": guard_config,
                "mode": args.mode if guard is not None else None,
                "metrics": [b.to_dict() for b in all_buckets],
                "trajectories": all_details,
                "table": table,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
