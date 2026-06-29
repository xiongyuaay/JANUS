#!/usr/bin/env python3
"""Stratified val split for the JANUS training data.

Reads one (or more) `train_converted_part_*.json` files, holds out a stratified
sample by (category, label), writes:
  - <out_dir>/val.json
  - <out_dir>/train_quick.json     (the input minus the val ids)

The remaining train file is what the smoke-test entry script points at. Source
files are never modified.

Usage:
    python scripts/split_val.py \
        --src "/path/to/janus/train/train_converted_part_*.json" \
        --out_dir /path/to/janus_dataset \
        --val_per_group 1 \
        --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import Counter, defaultdict
from typing import Any


def load_json(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a JSON list")
    return data


def stratified_split(
    samples: list[dict[str, Any]],
    val_per_group: int,
    min_per_class: int,
    train_cap: int,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stratify by (category, label).

    1. For each (category, label) bucket, hold out `val_per_group` samples for val.
    2. Boost minority labels in val to at least `min_per_class` total.
    3. Optionally cap the train set at `train_cap` total samples by sampling each
       (category, label) bucket proportionally (only if `train_cap > 0` and the
       remaining train pool exceeds it). Min 1 per non-empty bucket so we never
       lose category coverage.
    """
    rng = random.Random(seed)

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, s in enumerate(samples):
        cat = str(s.get("category", "unknown"))
        lbl = str(s.get("label", "unknown"))
        groups[(cat, lbl)].append(idx)

    val_indices: set[int] = set()
    for key, idxs in groups.items():
        rng.shuffle(idxs)
        take = min(val_per_group, len(idxs))
        for i in idxs[:take]:
            val_indices.add(i)

    # Make sure each label class hits at least `min_per_class` total samples in val.
    by_label: dict[str, list[int]] = defaultdict(list)
    for idx, s in enumerate(samples):
        by_label[str(s.get("label", "unknown"))].append(idx)

    for lbl, idxs in by_label.items():
        already = sum(1 for i in idxs if i in val_indices)
        if already >= min_per_class:
            continue
        remaining = [i for i in idxs if i not in val_indices]
        rng.shuffle(remaining)
        for i in remaining[: (min_per_class - already)]:
            val_indices.add(i)

    # Build the remaining train pool, then optionally subsample (capped, stratified).
    train_pool: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, s in enumerate(samples):
        if idx in val_indices:
            continue
        cat = str(s.get("category", "unknown"))
        lbl = str(s.get("label", "unknown"))
        train_pool[(cat, lbl)].append(idx)

    pool_total = sum(len(v) for v in train_pool.values())
    if train_cap > 0 and pool_total > train_cap:
        # Proportional allocation per bucket, with min 1 each.
        bucket_keys = list(train_pool.keys())
        for k in bucket_keys:
            rng.shuffle(train_pool[k])
        budgets: dict[tuple[str, str], int] = {}
        for k, idxs in train_pool.items():
            share = max(1, round(train_cap * len(idxs) / pool_total))
            budgets[k] = min(share, len(idxs))
        # Adjust to match cap exactly (small drift from rounding).
        diff = train_cap - sum(budgets.values())
        if diff != 0:
            order = sorted(bucket_keys, key=lambda k: -len(train_pool[k]))
            i = 0
            while diff != 0 and order:
                k = order[i % len(order)]
                if diff > 0 and budgets[k] < len(train_pool[k]):
                    budgets[k] += 1
                    diff -= 1
                elif diff < 0 and budgets[k] > 1:
                    budgets[k] -= 1
                    diff += 1
                i += 1
                if i > 10 * len(order):
                    break
        train_indices = []
        for k, idxs in train_pool.items():
            train_indices.extend(idxs[: budgets[k]])
        train_samples = [samples[i] for i in train_indices]
    else:
        train_samples = [s for i, s in enumerate(samples) if i not in val_indices]

    val_samples = [samples[i] for i in sorted(val_indices)]
    return train_samples, val_samples


def report(name: str, samples: list[dict[str, Any]]) -> None:
    labels = Counter(str(s.get("label")) for s in samples)
    cats = Counter(str(s.get("category")) for s in samples)
    print(f"[{name}] n={len(samples)}")
    print(f"  labels:     {dict(labels)}")
    print(f"  categories: {dict(cats)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--src",
        nargs="+",
        required=True,
        help="One or more train_converted_part_*.json files to split from.",
    )
    p.add_argument(
        "--out_dir",
        required=True,
        help="Output directory; will create val.json and train_quick.json there.",
    )
    p.add_argument(
        "--val_per_group",
        type=int,
        default=1,
        help="How many samples to hold out per (category, label) group.",
    )
    p.add_argument(
        "--min_per_class",
        type=int,
        default=20,
        help="Make sure each label class has at least this many samples in val.",
    )
    p.add_argument(
        "--train_cap",
        type=int,
        default=0,
        help="If >0, subsample the train set down to this many samples (stratified by (category,label)).",
    )
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    samples: list[dict[str, Any]] = []
    for src in args.src:
        cur = load_json(src)
        print(f"loaded {src}: {len(cur)} samples")
        samples.extend(cur)
    print(f"total source samples: {len(samples)}")

    train_samples, val_samples = stratified_split(
        samples,
        val_per_group=args.val_per_group,
        min_per_class=args.min_per_class,
        train_cap=args.train_cap,
        seed=args.seed,
    )

    report("train_quick", train_samples)
    report("val", val_samples)

    os.makedirs(args.out_dir, exist_ok=True)
    train_path = os.path.join(args.out_dir, "train_quick.json")
    val_path = os.path.join(args.out_dir, "val.json")
    with open(train_path, "w", encoding="utf-8") as f:
        json.dump(train_samples, f, ensure_ascii=False)
    with open(val_path, "w", encoding="utf-8") as f:
        json.dump(val_samples, f, ensure_ascii=False)
    print(f"\nwrote {train_path}")
    print(f"wrote {val_path}")


if __name__ == "__main__":
    main()
