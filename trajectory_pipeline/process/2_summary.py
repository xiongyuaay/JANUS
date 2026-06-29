from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI


# =============================================================================
# 配置（硬编码，按需修改）
# =============================================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

MODEL_NAME = os.environ.get("SUMMARY_MODEL", "Qwen/Qwen3-32B")
BASE_URL = os.environ.get("SUMMARY_BASE_URL", os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"))
API_KEY = os.environ.get("SUMMARY_API_KEY", os.environ.get("OPENAI_API_KEY", "EMPTY"))
TEMPERATURE = 0.0
TIMEOUT = 600.0
WORKERS = 32

# 输入：1_split.py 产出的 split 时间戳目录。留空则自动选最新一个。
INPUT_SPLIT_DIR: str | None = "eval_20260506_115713"
INPUT_ROOT = ROOT_DIR / "process" / "split"
OUTPUT_ROOT = ROOT_DIR / "process" / "summary"

OVERWRITE_EXISTING = True

# 续跑：指向 process/summary/<这个目录>。启用后脚本会从该目录的 summaries.json 读取，
# 只为 summary 为空的记录重新生成，并写回原文件（不新建时间戳目录）。
# 可由 CLI --resume <dir> 覆盖。留空表示从头开始。
RESUME_FROM: str | None = None

SYSTEM_PROMPT = """You summarize agent trajectories in English.

Requirements:
- Output only the summary text.
- Use English.
- Focus on the main actions, tool usage, and final outcome.
- Do not add bullet points, titles, or markdown.
- Do not invent details that are not present in the trajectory.""".strip()


# =============================================================================
# IO
# =============================================================================

def resolve_input_dir(input_split_dir: str | None) -> Path:
    selected = input_split_dir if input_split_dir is not None else INPUT_SPLIT_DIR
    if selected:
        path = INPUT_ROOT / selected
        if not path.exists():
            raise FileNotFoundError(f"input dir not found: {path}")
        return path
    candidates = sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise FileNotFoundError(f"no split runs under {INPUT_ROOT}")
    return candidates[-1]


def load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list.")
    return data


def dump_json(path: Path, data: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# =============================================================================
# 模型调用
# =============================================================================

def build_user_prompt(trajectory: str) -> str:
    return f"""Summarize the following trajectory in English.

Trajectory:
{trajectory}""".strip()


def _strip_thinking(raw: str) -> str:
    cleaned = re.sub(r"<(?:think|reason)>.*?</(?:think|reason)>", "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"^.*?</(?:think|reason)>", "", cleaned, flags=re.DOTALL)
    if cleaned.startswith("Thinking Process:"):
        first_json = cleaned.find("{")
        if first_json != -1:
            cleaned = cleaned[first_json:]
    return cleaned.strip()


def summarize_text(client: OpenAI, trajectory: str) -> str:
    response = client.chat.completions.create(
        model=MODEL_NAME,
        temperature=TEMPERATURE,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(trajectory)},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    message = response.choices[0].message.content or ""
    return _strip_thinking(message)


def summarize_record(index: int, record: dict[str, Any], overwrite: bool) -> tuple[int, str]:
    current_summary = record.get("summary", "")
    if not overwrite and isinstance(current_summary, str) and current_summary.strip():
        return index, current_summary.strip()
    trajectory = record.get("trajectory_2", "")
    if not isinstance(trajectory, str) or not trajectory.strip():
        return index, ""
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=TIMEOUT)
    try:
        summary = summarize_text(client, trajectory)
    except Exception as exc:
        print(f"failed index={index} error={exc}", flush=True)
        return index, current_summary.strip() if isinstance(current_summary, str) else ""
    return index, summary


# =============================================================================
# 主流程
# =============================================================================

def make_output_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = OUTPUT_ROOT / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize trajectories; supports resume.")
    parser.add_argument(
        "--input-split-dir",
        type=str,
        default=None,
        help=(
            "Read process/split/<dir>/splits_*.json (one per dataset; falls back to splits.json) "
            "for a fresh summary run. Default: latest split dir."
        ),
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=RESUME_FROM,
        help=(
            "Resume from an existing summary timestamp directory under process/summary/. "
            "The script will load that directory's summaries_*.json (or legacy summaries.json), "
            "skip records that already have a non-empty summary, and write updates back to the SAME files."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=WORKERS,
        help="Number of concurrent summarization requests.",
    )
    return parser.parse_args()


@dataclass
class DatasetWork:
    name: str
    input_path: Path
    output_path: Path
    data: list[dict[str, Any]]
    pending_indices: list[int] = field(default_factory=list)


def discover_split_files(input_dir: Path) -> list[tuple[str, Path]]:
    """Find splits_<name>.json files in the input dir.

    Falls back to the legacy single 'splits.json' if no per-dataset files exist."""
    multi = sorted(input_dir.glob("splits_*.json"))
    if multi:
        out: list[tuple[str, Path]] = []
        for p in multi:
            name = p.stem[len("splits_"):]
            if name:
                out.append((name, p))
        return out
    legacy = input_dir / "splits.json"
    if legacy.exists():
        return [("all", legacy)]
    return []


def discover_summary_files(resume_dir: Path) -> list[tuple[str, Path]]:
    multi = sorted(resume_dir.glob("summaries_*.json"))
    if multi:
        return [(p.stem[len("summaries_"):], p) for p in multi if p.stem != "summaries_"]
    legacy = resume_dir / "summaries.json"
    if legacy.exists():
        return [("all", legacy)]
    return []


def main() -> None:
    args = parse_args()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if args.resume and args.input_split_dir:
        raise ValueError("--resume and --input-split-dir cannot be used together")

    works: list[DatasetWork] = []

    if args.resume:
        # --- Resume mode: reuse existing summaries_*.json in place ---
        resume_dir = OUTPUT_ROOT / args.resume
        files = discover_summary_files(resume_dir)
        if not files:
            raise FileNotFoundError(f"cannot resume, no summaries_*.json under: {resume_dir}")
        output_dir = resume_dir
        overwrite = False
        for name, path in files:
            data = load_json(path)
            pending = [
                i for i, r in enumerate(data)
                if not (isinstance(r.get("summary"), str) and r["summary"].strip())
            ]
            already = len(data) - len(pending)
            print(
                f"[resume][{name}] file={path.name} total={len(data)} already_done={already} pending={len(pending)}",
                flush=True,
            )
            works.append(DatasetWork(name=name, input_path=path, output_path=path, data=data, pending_indices=pending))
    else:
        # --- Fresh run: read splits_*.json, create new timestamp output dir ---
        input_dir = resolve_input_dir(args.input_split_dir)
        files = discover_split_files(input_dir)
        if not files:
            raise FileNotFoundError(f"no splits_*.json or splits.json found under: {input_dir}")
        output_dir = make_output_dir()
        overwrite = OVERWRITE_EXISTING
        for name, in_path in files:
            data = load_json(in_path)
            for record in data:
                record.setdefault("summary", "")
            out_name = "summaries.json" if name == "all" else f"summaries_{name}.json"
            out_path = output_dir / out_name
            print(f"input[{name}]={in_path} records={len(data)} output={out_path}", flush=True)
            works.append(
                DatasetWork(
                    name=name,
                    input_path=in_path,
                    output_path=out_path,
                    data=data,
                    pending_indices=list(range(len(data))),
                )
            )

    total_pending = sum(len(w.pending_indices) for w in works)
    total_records = sum(len(w.data) for w in works)

    if total_pending == 0:
        for w in works:
            dump_json(w.output_path, w.data)
        print("nothing to do; all records already summarized.", flush=True)
        return

    # Single shared worker pool across all datasets.
    futures_meta: dict[Any, tuple[int, int]] = {}  # future -> (work_idx, record_idx)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for w_idx, w in enumerate(works):
            for r_idx in w.pending_indices:
                fut = executor.submit(summarize_record, r_idx, w.data[r_idx], overwrite)
                futures_meta[fut] = (w_idx, r_idx)
        done_per_work = [0] * len(works)
        done_total = 0
        for future in as_completed(futures_meta):
            w_idx, r_idx = futures_meta[future]
            _, summary = future.result()
            w = works[w_idx]
            w.data[r_idx]["summary"] = summary
            done_per_work[w_idx] += 1
            done_total += 1
            if done_per_work[w_idx] % 50 == 0 or done_per_work[w_idx] == len(w.pending_indices):
                dump_json(w.output_path, w.data)
            print(
                f"summary[{w.name}] {done_per_work[w_idx]}/{len(w.pending_indices)} "
                f"(global {done_total}/{total_pending})",
                flush=True,
            )

    for w in works:
        dump_json(w.output_path, w.data)

    summary_path = output_dir / "summary.json"
    summary_meta: dict[str, Any] = {}
    if summary_path.exists():
        previous_meta = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(previous_meta, dict):
            summary_meta.update(previous_meta)
    now = datetime.now().isoformat()
    summary_meta.setdefault("generated_at", now)
    per_dataset_records = {w.name: len(w.data) for w in works}
    per_dataset_with_summary = {
        w.name: sum(1 for r in w.data if isinstance(r.get("summary"), str) and r["summary"].strip())
        for w in works
    }
    output_files = {w.name: str(w.output_path) for w in works}
    summary_meta.update(
        {
            "summarized_at": now,
            "resumed_from": args.resume or None,
            "model": MODEL_NAME,
            "workers": args.workers,
            "total_records": total_records,
            "with_summary": sum(per_dataset_with_summary.values()),
            "per_dataset_records": per_dataset_records,
            "per_dataset_with_summary": per_dataset_with_summary,
            "output_files": output_files,
        }
    )
    summary_path.write_text(
        json.dumps(summary_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 60)
    print(f"total_records: {summary_meta['total_records']}")
    print(f"with_summary:  {summary_meta['with_summary']}")
    print(f"per_dataset:   {per_dataset_with_summary}")
    for name, p in output_files.items():
        print(f"output[{name}]: {p}")


if __name__ == "__main__":
    main()
