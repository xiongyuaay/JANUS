"""Unified entrypoint orchestration."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as _dt
import time
from pathlib import Path
from typing import Any

from .agent import UnifiedReActAgent
from .benchmarks import build_adapter
from .benchmarks.common import BenchmarkRunOutput
from .config import EvalConfig
from .guards import build_guard
from .model_client import build_chat_client
from .progress import banner, error as log_error, info, is_quiet
from .summary_report import build_rows, render_table, write_csv
from .utils import ensure_dir, write_json


def normalize_benchmark_key(name: str) -> str:
    key = name.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "agent_safety_bench": "agent_safetybench",
        "agentsafetybench": "agent_safetybench",
        "agent_dojo": "agentdojo",
        "agent_harm": "agentharm",
        "agent_lab": "agentlab",
        "lps": "lps_bench",
    }
    return aliases.get(key, key)


def _log_benchmark_issues(name: str, result: Any) -> None:
    metrics = result.metrics if isinstance(getattr(result, "metrics", None), dict) else {}
    status = str(getattr(result, "status", "") or metrics.get("status") or "")
    if status and status not in {"completed", "resumed"}:
        reason = metrics.get("reason") or metrics.get("error") or ""
        log_error(f"[error] benchmark={name} status={status}" + (f" reason={reason}" if reason else ""))
    _log_metric_issues(name, metrics)


def _log_metric_issues(prefix: str, metrics: dict[str, Any]) -> None:
    reason = metrics.get("reason")
    if reason and str(metrics.get("status", "")) not in {"", "completed", "resumed"}:
        log_error(f"[error] {prefix}: {reason}")

    failed_suites = metrics.get("failed_suites")
    if isinstance(failed_suites, list):
        for item in failed_suites:
            if isinstance(item, dict):
                suite = item.get("suite", "?")
                err = item.get("error") or item.get("reason") or ""
                log_error(f"[error] {prefix}: suite={suite} {err}")

    for key in ("judge_error_count", "format_error_count", "run_error_count"):
        value = metrics.get(key)
        if isinstance(value, int) and value > 0:
            log_error(f"[error] {prefix}: {key}={value}")

    runs = metrics.get("runs")
    if isinstance(runs, dict):
        for run_name, run_data in runs.items():
            if not isinstance(run_data, dict):
                continue
            run_status = str(run_data.get("status", ""))
            if run_status and run_status not in {"completed", "resumed"}:
                reason = run_data.get("reason") or run_data.get("error") or ""
                log_error(
                    f"[error] {prefix}/{run_name}: status={run_status}"
                    + (f" reason={reason}" if reason else "")
                )
            run_metrics = run_data.get("metrics")
            if isinstance(run_metrics, dict):
                _log_metric_issues(f"{prefix}/{run_name}", run_metrics)
    elif isinstance(runs, list):
        for idx, run_data in enumerate(runs, start=1):
            if not isinstance(run_data, dict):
                continue
            run_name = str(run_data.get("attack") or run_data.get("name") or f"run_{idx}")
            run_status = str(run_data.get("status", ""))
            if run_status and run_status not in {"completed", "resumed"}:
                reason = run_data.get("reason") or run_data.get("error") or ""
                log_error(
                    f"[error] {prefix}/{run_name}: status={run_status}"
                    + (f" reason={reason}" if reason else "")
                )


def run_from_config(cfg: EvalConfig) -> dict[str, Any]:
    run_cfg = dict(cfg.get("run", {}) or {})
    run_cfg.setdefault("defense", cfg.selected_defense)
    model_cfg = dict(cfg.get("model", {}) or {})
    run_cfg["model_name"] = model_cfg.get("name", "")
    run_cfg["model_base_url"] = model_cfg.get("base_url", "")
    run_cfg["model_api_key"] = model_cfg.get("api_key", "")

    model = build_chat_client(model_cfg)

    # Resolve the per-defense detail block. The new layout is
    #   defenses:
    #     <defense_name>:
    #       guard: { provider, name, base_url, api_key, threshold, ... }
    # The top-level ``guard:`` block is kept as a backward-compatible fallback
    # for existing configs that haven't migrated yet.
    defense_cfg = dict(cfg.get(f"defenses.{cfg.selected_defense}", {}) or {})
    guard_cfg = dict(defense_cfg.get("guard") or cfg.get("guard", {}) or {})
    guard = build_guard(cfg.selected_defense, guard_cfg)
    agent = UnifiedReActAgent(
        model=model,
        defense=cfg.selected_defense,
        guard=guard,
        max_turns=int(cfg.get("agent.max_turns", 10)),
    )
    run_cfg["defense_cfg"] = defense_cfg
    # Resume support: when run.resume_dir is set, reuse that exact directory
    # instead of building a new timestamped one. Each benchmark adapter then
    # decides how to skip work that has already been recorded there.
    resume_raw = cfg.get("run.resume_dir") or None
    resumed = False
    if resume_raw:
        resume_path = Path(str(resume_raw))
        if not resume_path.is_absolute():
            resume_path = (cfg.project_root / resume_path).resolve()
        out_root = resume_path
        resumed = True
    else:
        out_root = cfg.output_dir
        if bool(cfg.get("run.timestamp_output_dir", True)):
            timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_root = out_root / f"{cfg.get('run.experiment_name','unified_eval')}_{timestamp}"
    ensure_dir(out_root)
    run_cfg["resume"] = resumed
    run_cfg["output_root"] = str(out_root)
    rerun_errors = bool(run_cfg.get("rerun_errors", False))
    max_workers = int(cfg.get("run.max_workers", 1))
    selected = [normalize_benchmark_key(n) for n in cfg.selected_benchmarks]
    benchmark_workers = int(cfg.get("run.benchmark_workers", len(selected)) or 1)
    benchmark_workers = max(1, min(benchmark_workers, len(selected) or 1))
    # Surface max_workers so post-execution steps (e.g. agent_safetybench's
    # inline ShieldAgent judge over all trajectories) can parallelize too.
    run_cfg["max_workers"] = max_workers
    run_cfg["benchmark_workers"] = benchmark_workers
    benchmarks_cfg = dict(cfg.get("benchmarks", {}) or {})
    summary: dict[str, Any] = {
        "experiment_name": cfg.get("run.experiment_name", "unified_eval"),
        "config_path": str(cfg.path),
        "output_dir": str(out_root),
        "defense": cfg.selected_defense,
        "model": model_cfg.get("name"),
        "max_workers": max_workers,
        "benchmark_workers": benchmark_workers,
        "resumed": resumed,
        "rerun_errors": rerun_errors,
        "rerun_error_patterns": run_cfg.get("rerun_error_patterns", []),
        "benchmarks": {},
    }
    banner(
        f"Unified eval{' (RESUME)' if resumed else ''}: {len(selected)} benchmark(s) → "
        f"{', '.join(selected)} | defense={cfg.selected_defense} | out={out_root}"
        + (" | rerun_errors=on" if resumed and rerun_errors else "")
    )

    def run_benchmark(idx: int, name: str) -> Any:
        bench_run_cfg = dict(run_cfg)
        bench_run_cfg["_rerun_case_ids_by_output"] = {}
        bench_out = out_root / name / cfg.selected_defense
        banner(f"[{idx}/{len(selected)}] benchmark={name} starting")
        t0 = time.time()
        try:
            adapter = build_adapter(name, cfg.project_root, benchmarks_cfg, bench_run_cfg)
            result = adapter.run(agent, bench_out, max_workers=max_workers)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            metrics = {"status": "run_failed", "reason": reason}
            result = BenchmarkRunOutput(name, bench_out, "run_failed", metrics, 0)
            write_json(bench_out / "metrics.json", metrics)
        info(
            f"[{idx}/{len(selected)}] benchmark={name} {result.status} "
            f"cases={result.num_cases} elapsed={time.time() - t0:.1f}s -> {result.output_dir}"
        )
        _log_benchmark_issues(name, result)
        return result

    def add_result(name: str, result: Any) -> None:
        summary["benchmarks"][name] = {
            "status": result.status,
            "output_dir": str(result.output_dir),
            "num_cases": result.num_cases,
            "metrics": result.metrics,
        }
        write_json(out_root / "summary.json", summary)

    results: dict[str, Any] = {}
    if benchmark_workers == 1 or len(selected) <= 1:
        for idx, name in enumerate(selected, start=1):
            result = run_benchmark(idx, name)
            results[name] = result
            add_result(name, result)
    else:
        with ThreadPoolExecutor(max_workers=benchmark_workers) as pool:
            futures = {
                pool.submit(run_benchmark, idx, name): name
                for idx, name in enumerate(selected, start=1)
            }
            for future in as_completed(futures):
                name = futures[future]
                result = future.result()
                results[name] = result
                add_result(name, result)

    summary["benchmarks"] = {}
    for name in selected:
        result = results[name]
        add_result(name, result)
    write_json(out_root / "summary.json", summary)

    # Aggregate every benchmark's official metrics into one flat table:
    # per-benchmark rows for utility / ASR / official-score / refuse_rate.
    # Benign-only benchmarks fill ASR with "-"; pure attack benchmarks fill
    # utility with "-". CSV mirrors the printed table exactly.
    rows = build_rows(summary, cfg.selected_defense)
    csv_path = out_root / "summary_table.csv"
    write_csv(rows, csv_path)
    summary["summary_table"] = rows
    summary["summary_csv"] = str(csv_path)
    write_json(out_root / "summary.json", summary)

    table = render_table(rows)
    banner(f"Unified eval finished. Output: {out_root}")
    if not is_quiet():
        print()
        print("=== Final results ===")
        print(table)
    info(f"summary table written to {csv_path}")
    return summary
