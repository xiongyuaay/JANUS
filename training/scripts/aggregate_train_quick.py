"""Aggregate per-run train_converted_*.json files into a single train_quick.json."""

import json
from pathlib import Path

# ===== Config (hardcoded) =====
INPUT_DIR = Path("data/train/v5.18_testv2_v2")
OUTPUT_FILE = INPUT_DIR / "train_quick.json"
TRAIN_GLOB = "train_converted_*.json"
RENUMBER_IDS = True
# ==============================


def main():
    if not INPUT_DIR.is_dir():
        raise FileNotFoundError(f"Input directory not found: {INPUT_DIR}")

    subdirs = sorted(p for p in INPUT_DIR.iterdir() if p.is_dir())
    if not subdirs:
        raise RuntimeError(f"No subdirectories under {INPUT_DIR}")

    aggregated = []
    next_id = 1
    for sub in subdirs:
        train_files = sorted(sub.glob(TRAIN_GLOB))
        if not train_files:
            print(f"[skip] no train files in {sub}")
            continue
        for train_file in train_files:
            with train_file.open("r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError(f"Expected list in {train_file}, got {type(records).__name__}")
            for rec in records:
                if RENUMBER_IDS:
                    rec["id"] = next_id
                    next_id += 1
                aggregated.append(rec)
            print(f"[load] {train_file}  (+{len(records)})")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(aggregated, f, ensure_ascii=False)

    print(f"[done] wrote {len(aggregated)} records -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
