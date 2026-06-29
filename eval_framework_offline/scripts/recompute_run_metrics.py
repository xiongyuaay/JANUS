#!/usr/bin/env python3
"""Recompute metrics inside existing run JSONs using current data semantics.

This is useful after repairing eval_data labels or changing metric logic.
It preserves the original per-trajectory guard decisions (`flagged`,
`first_block_step`, `step_labels`) and only refreshes:

* trajectory-level `benchmark` / `utility` / `security`
* bucket-level `metrics`
* rendered `table`

By default the script writes corrected copies under `<runs_root>_corrected/`.
Use `--in-place` to overwrite the original run JSONs.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval_framework import metrics  # noqa: E402
from eval_framework.aggregator import TrajectoryResult  # noqa: E402
from eval_framework.cli import _render_metrics_table  # noqa: E402
from eval_framework.loaders import get_loader  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--runs-root", type=Path, required=True)
    p.add_argument(
        "--run-glob",
        default="*.json",
        help="Glob pattern under --runs-root (default: *.json).",
    )
    p.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="Directory for corrected copies. Defaults to <runs_root>_corrected.",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the original run JSONs in-place.",
    )
    return p.parse_args()


def load_lookup(
    data_root: Path,
    benchmarks: list[str],
    splits: list[str],
    model_filter: Optional[list[str]],
):
    by_key: dict[tuple[str, str, str, Optional[str]], list] = defaultdict(list)
    for bench in benchmarks:
        loader = get_loader(bench)
        effective_filter = model_filter if loader.has_model_dim else None
        for traj in loader.iter_trajectories(
            data_root,
            splits=splits,  # type: ignore[arg-type]
            models=effective_filter,
        ):
            by_key[(traj.id, traj.split, traj.benchmark, traj.model)].append(traj)
    return by_key


def resolve_trajectory(detail: dict, lookup: dict, benchmarks: list[str]):
    bench = detail.get("benchmark")
    key_candidates = []
    if bench is not None:
        key_candidates.append((detail["trajectory_id"], detail["split"], bench, detail.get("model")))
    else:
        for b in benchmarks:
            key_candidates.append((detail["trajectory_id"], detail["split"], b, detail.get("model")))

    candidates = []
    for key in key_candidates:
        candidates.extend(lookup.get(key, []))

    if not candidates:
        raise KeyError(
            f"could not resolve trajectory {detail['trajectory_id']!r} "
            f"split={detail['split']!r} model={detail.get('model')!r}"
        )
    if len(candidates) > 1:
        uniq = {(c.benchmark, c.id, c.split, c.model) for c in candidates}
        if len(uniq) > 1:
            raise KeyError(
                f"ambiguous trajectory {detail['trajectory_id']!r}: "
                f"{sorted(uniq)}"
            )
    return candidates[0]


def recompute_run(run_path: Path, data_root: Path, out_path: Path) -> bool:
    raw = json.loads(run_path.read_text(encoding="utf-8"))

    if not isinstance(raw.get("metrics"), list) or not isinstance(raw.get("trajectories"), list):
        print(f"skip nonstandard run file: {run_path.name}")
        return False

    benchmarks = list(raw.get("benchmarks") or [])
    if not benchmarks:
        print(f"skip missing benchmarks: {run_path.name}")
        return False

    splits = list(raw.get("splits") or ["benign", "adversarial"])
    model_filter = raw.get("models_filter")
    if model_filter is not None:
        model_filter = list(model_filter)

    lookup = load_lookup(data_root, benchmarks, splits, model_filter)
    guard_applied = raw.get("guard") is not None
    mode = raw.get("mode")

    results_by_bucket: dict[tuple[str, Optional[str]], list[TrajectoryResult]] = defaultdict(list)

    for detail in raw["trajectories"]:
        traj = resolve_trajectory(detail, lookup, benchmarks)
        detail["benchmark"] = traj.benchmark
        detail["utility"] = traj.utility
        detail["security"] = traj.security

        res = TrajectoryResult(
            trajectory_id=traj.id,
            benchmark=traj.benchmark,
            split=traj.split,
            model=traj.model,
            utility=traj.utility,
            security=traj.security,
            num_steps=detail.get("num_steps", len(traj.steps)),
            flagged=bool(detail.get("flagged")),
            first_block_step=detail.get("first_block_step"),
            step_verdicts=[],
            step_labels=list(detail.get("step_labels") or []),
        )
        results_by_bucket[(traj.benchmark, traj.model)].append(res)

    all_buckets: list[metrics.BucketMetrics] = []
    for bench in benchmarks:
        seen_models = sorted(
            {model for (b, model) in results_by_bucket if b == bench},
            key=lambda x: (x is None, x or ""),
        )
        for model in seen_models:
            bucket = metrics.compute(
                bench,
                model,
                results_by_bucket[(bench, model)],
                mode=mode,
                guard_applied=guard_applied,
            )
            all_buckets.append(bucket)

    raw["metrics"] = [b.to_dict() for b in all_buckets]
    raw["table"] = _render_metrics_table(all_buckets, guard_applied=guard_applied)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    return True


def main() -> int:
    args = parse_args()
    out_root = args.runs_root if args.in_place else (args.out_root or args.runs_root.parent / f"{args.runs_root.name}_corrected")

    updated = 0
    for run_path in sorted(args.runs_root.glob(args.run_glob)):
        out_path = run_path if args.in_place else out_root / run_path.name
        updated += int(recompute_run(run_path, args.data_root, out_path))

    print(f"updated {updated} run file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
