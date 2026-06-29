from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


# =============================================================================
# 配置（硬编码，按需修改）
# =============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

# 输入：3_anotation.py 产出的 anotation 时间戳目录。留空则自动选最新一个。
INPUT_ANOTATION_DIR: str | None = "20260506_112926"
INPUT_ROOT = ROOT_DIR / "process" / "anotation"
OUTPUT_ROOT = ROOT_DIR / "process" / "sample" / "train"

# 各标签需要采样的样本数量。
SAMPLE_PER_LABEL: dict[str, int] = {
    "safe": 5201,
    "potential unsafe": 5105,
    "unsafe": 5951,
}

# 是否要求样本必须有非空 summary。
REQUIRE_SUMMARY = True

SEED = 42

VALID_LABELS = ("safe", "potential unsafe", "unsafe")


# =============================================================================
# IO
# =============================================================================

def resolve_input_dir() -> Path:
    if INPUT_ANOTATION_DIR:
        path = INPUT_ROOT / INPUT_ANOTATION_DIR
        if not path.exists():
            raise FileNotFoundError(f"input dir not found: {path}")
        return path
    candidates = sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise FileNotFoundError(f"no anotation runs under {INPUT_ROOT}")
    return candidates[-1]


def load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list.")
    return data


# =============================================================================
# 转换
# =============================================================================

def convert_record(record: dict[str, Any], new_id: int) -> dict[str, Any]:
    """转换为 data/train/train_converted_part_1.json 同样的字段结构。"""
    return {
        "id": new_id,
        "instruction": record.get("instruction", ""),
        "trajectory_1": record.get("trajectory_1", ""),
        "trajectory_2": record.get("trajectory_2", ""),
        "label": record.get("label", ""),
        "label_reason": record.get("label_reason", ""),
        "category": record.get("category", "unknown"),
        "summary": record.get("summary", ""),
    }


def sample_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        label = record.get("label")
        if label not in VALID_LABELS:
            continue
        if REQUIRE_SUMMARY:
            summary = record.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                continue
        by_label[label].append(record)

    selected: list[dict[str, Any]] = []
    for label in VALID_LABELS:
        target = SAMPLE_PER_LABEL.get(label, 0)
        pool = by_label.get(label, [])
        rng.shuffle(pool)
        chosen = pool[:target]
        selected.extend(chosen)
        print(
            f"[label={label}] target={target} available={len(pool)} chosen={len(chosen)}",
            flush=True,
        )

    rng.shuffle(selected)
    return [convert_record(record, idx) for idx, record in enumerate(selected, start=1)]


def make_output_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    input_dir = resolve_input_dir()
    output_dir = make_output_dir()

    annotations_path = input_dir / "annotations.json"
    records = load_json(annotations_path)
    print(f"input={annotations_path} records={len(records)} output_dir={output_dir}", flush=True)

    converted = sample_records(records)
    output_path = output_dir / "train_converted_part_1.json"
    output_path.write_text(json.dumps(converted, ensure_ascii=False, indent=2), encoding="utf-8")

    label_counter = Counter(r["label"] for r in converted)
    summary_meta = {
        "generated_at": datetime.now().isoformat(),
        "input_dir": str(input_dir.relative_to(ROOT_DIR)),
        "sample_per_label": SAMPLE_PER_LABEL,
        "require_summary": REQUIRE_SUMMARY,
        "total": len(converted),
        "by_label": {label: label_counter.get(label, 0) for label in VALID_LABELS},
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 60)
    print(f"total: {len(converted)}")
    for label in VALID_LABELS:
        print(f"  {label}: {label_counter.get(label, 0)}")
    print(f"output: {output_path}")


if __name__ == "__main__":
    main()
