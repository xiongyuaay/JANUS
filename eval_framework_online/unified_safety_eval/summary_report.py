"""Post-run summary table.

Walks ``summary["benchmarks"]`` (the dict produced by ``runner.run_from_config``)
and produces one flattened row per benchmark/run pair. Each benchmark has its
own extractor that knows how to pull official metrics out of that benchmark's
specific schema.

Reporting policy (per the task brief):

* If a benchmark has a benign run, the row reports both ``utility`` and ``ASR``
  (utility on benign rows only, ASR on attack rows only — they are filled in
  from the benchmark's own metrics when applicable).
* If a benchmark has no benign data, only ``ASR`` is reported.
* Each benchmark's official score (e.g. AgentHarm rubric mean, LPS-Bench
  pass_rate) is surfaced in the ``score`` column alongside utility/ASR.

Output:

* ``render_table(rows)`` — monospace text table for stdout.
* ``write_csv(rows, path)`` — CSV with the same columns.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Callable

# Column order is fixed so CSV / table layouts stay stable across runs.
COLUMNS: list[str] = [
    "benchmark",
    "run",
    "defense",
    "num_cases",
    "utility",
    "ASR",
    "score",
    "refuse_rate",
    "status",
]


# --------------------------------------------------------------------- helpers
def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _frac(value: Any) -> float | None:
    """Return ``value`` as a fraction in ``[0, 1]`` if numeric, else None.

    AgentLAB's aggregate emits percentages (0..100). Everything else is already
    in fractions. We normalise to fractions so the table is consistent.
    """
    if not _is_num(value):
        return None
    f = float(value)
    if math.isnan(f):
        return None
    if f > 1.0:
        return f / 100.0
    return f


def _fmt_num(value: Any) -> str:
    """Format a number to 4 decimals; missing/NaN -> ``-``."""
    if value is None:
        return "-"
    if not _is_num(value):
        return str(value)
    f = float(value)
    if math.isnan(f):
        return "-"
    return f"{f:.4f}"


def _fmt_int(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if _is_num(value):
        return str(int(value))
    return str(value)


def _row(
    *,
    benchmark: str,
    run: str,
    defense: str,
    num_cases: Any,
    utility: Any,
    asr: Any,
    score: Any,
    refuse_rate: Any,
    status: Any,
) -> dict[str, Any]:
    return {
        "benchmark": benchmark,
        "run": run,
        "defense": defense,
        "num_cases": num_cases,
        "utility": utility,
        "ASR": asr,
        "score": score,
        "refuse_rate": refuse_rate,
        "status": status or "completed",
    }


# ---------------------------------------------------------- per-benchmark rows
def _agentharm_rows(name: str, info: dict, defense: str) -> list[dict]:
    """AgentHarm: separate benign + harmful runs with rubric-based scoring.

    benign  -> utility = ``all_components_pass_rate``, score = ``mean_component_score``
    harmful -> ASR     = ``harmful_completion_rate_proxy``, score = ``mean_component_score``
    """
    metrics = info.get("metrics") or {}
    runs = metrics.get("runs") or {}
    rows: list[dict] = []
    for run_name, run_info in runs.items():
        m = run_info.get("metrics") or {}
        subset = str(m.get("subset", run_name)).lower()
        is_benign = subset == "benign"
        rows.append(
            _row(
                benchmark=name,
                run=run_name,
                defense=defense,
                num_cases=m.get("num_cases") or run_info.get("num_cases"),
                utility=m.get("all_components_pass_rate") if is_benign else None,
                asr=None if is_benign else m.get("harmful_completion_rate_proxy"),
                score=m.get("mean_component_score"),
                refuse_rate=m.get("refusal_or_block_rate"),
                status=run_info.get("status"),
            )
        )
    if not rows:
        rows.append(
            _row(
                benchmark=name, run="all", defense=defense,
                num_cases=info.get("num_cases"),
                utility=None, asr=None, score=None, refuse_rate=None,
                status=info.get("status"),
            )
        )
    return rows


def _agentdojo_rows(name: str, info: dict, defense: str) -> list[dict]:
    """AgentDojo: benign run reports utility (BU/CU); attack run reports UA + targeted_ASR.

    targeted_ASR = mean(security_results) per util_scripts/create_results_table.py.
    """
    metrics = info.get("metrics") or {}
    runs = metrics.get("runs") or {}
    rows: list[dict] = []
    for run_name, run_info in runs.items():
        run_metrics = run_info.get("metrics") or {}
        combined = run_metrics.get("combined") or {}
        m = combined or run_metrics
        is_benign = (
            "BU_benign_utility" in m
            or "CU_clean_utility" in m
            or str(run_metrics.get("attack_type", "")).lower() in {"none", "", "null"}
        )
        utility = (
            m.get("BU_benign_utility")
            or m.get("CU_clean_utility")
            or m.get("UA_utility_under_attack")
            or m.get("avg_utility")
        )
        asr = None if is_benign else m.get("targeted_ASR", m.get("ASR_attack_success_rate"))
        rows.append(
            _row(
                benchmark=name,
                run=run_name,
                defense=defense,
                num_cases=m.get("num_utility_cases") or run_info.get("num_cases"),
                utility=utility,
                asr=asr,
                score=None,
                refuse_rate=None,
                status=run_info.get("status"),
            )
        )
    if not rows:
        rows.append(
            _row(
                benchmark=name, run="all", defense=defense,
                num_cases=info.get("num_cases"),
                utility=None, asr=None, score=None, refuse_rate=None,
                status=info.get("status"),
            )
        )
    return rows


def _asb_rows(name: str, info: dict, defense: str) -> list[dict]:
    """ASB: official metrics from main_attacker.py — ASR, original_task_success_rate, refuse_rate.

    Older runs used PNA_/RR_/ASR_attack_success_rate names; we read both schemas
    so historical summary.json files render correctly.
    """
    metrics = info.get("metrics") or {}
    runs = metrics.get("runs") or {}
    rows: list[dict] = []
    for run_name, run_info in runs.items():
        m = run_info.get("metrics") or {}
        attack_type = str(m.get("attack_type") or "").upper()
        is_normal = attack_type in {"NONE", "", "NULL", "NORMAL", "CLEAN"}
        utility = (
            m.get("original_task_success_rate")
            if m.get("original_task_success_rate") is not None
            else m.get("PNA_original_task_success")
        )
        asr = (
            m.get("ASR") if m.get("ASR") is not None else m.get("ASR_attack_success_rate")
        )
        refuse = (
            m.get("refuse_rate")
            if m.get("refuse_rate") is not None
            else m.get("RR_refusal_rate")
        )
        rows.append(
            _row(
                benchmark=name,
                run=run_name,
                defense=defense,
                num_cases=m.get("num_cases") or run_info.get("num_cases"),
                utility=utility,
                asr=None if is_normal else asr,
                score=None,
                refuse_rate=refuse,
                status=run_info.get("status"),
            )
        )
    if not rows:
        rows.append(
            _row(
                benchmark=name, run="all", defense=defense,
                num_cases=info.get("num_cases"),
                utility=None, asr=None, score=None, refuse_rate=None,
                status=info.get("status"),
            )
        )
    return rows


def _lps_bench_rows(name: str, info: dict, defense: str) -> list[dict]:
    """LPS-Bench: no benign data. ``score`` = pass_rate from official LLM judge.

    We expose ``ASR = 1 - pass_rate`` as a convenience (fraction of cases the
    judge marked unsafe / failed). When the judge wasn't run, both fields are
    blank.
    """
    m = info.get("metrics") or {}
    pass_rate = m.get("pass_rate") if isinstance(m.get("pass_rate"), (int, float)) else None
    asr = (1.0 - pass_rate) if pass_rate is not None else None
    return [
        _row(
            benchmark=name, run="all", defense=defense,
            num_cases=m.get("num_cases") or info.get("num_cases"),
            utility=None,
            asr=asr,
            score=pass_rate,
            refuse_rate=None,
            status=m.get("status") or info.get("status"),
        )
    ]


def _agent_safetybench_rows(name: str, info: dict, defense: str) -> list[dict]:
    """Agent-SafetyBench: trajectory-only by default. ShieldAgent score (when run)
    appears under ``shield_scoring_status``; we surface it as the row status."""
    m = info.get("metrics") or {}
    return [
        _row(
            benchmark=name, run="all", defense=defense,
            num_cases=m.get("num_cases") or info.get("num_cases"),
            utility=None,
            asr=None,
            score=None,
            refuse_rate=None,
            status=m.get("shield_scoring_status") or info.get("status") or m.get("status"),
        )
    ]


def _agentlab_rows(name: str, info: dict, defense: str) -> list[dict]:
    """AgentLAB: per-track ASR pulled from each script's final_results.json.

    Tracks are pure attack runs (no benign), so utility is always blank.
    Aggregates `aggregate.micro_asr_percent` into a final ``AGGREGATE`` row.
    """
    metrics = info.get("metrics") or {}
    rows: list[dict] = []
    for rec in metrics.get("runs") or []:
        attack = rec.get("attack") or "?"
        summaries = rec.get("summaries") or []
        # Pick the first summary that exposes any of asr/success_rate/rate.
        s = next(
            (
                s
                for s in summaries
                if any(_is_num(s.get(k)) for k in ("asr", "success_rate", "rate"))
            ),
            {},
        )
        asr_val = None
        for key in ("asr", "success_rate", "rate"):
            if _is_num(s.get(key)):
                asr_val = _frac(s[key])
                break
        rows.append(
            _row(
                benchmark=name,
                run=str(attack),
                defense=defense,
                num_cases=s.get("total") or s.get("tested") or info.get("num_cases"),
                utility=None,
                asr=asr_val,
                score=None,
                refuse_rate=None,
                status=rec.get("status"),
            )
        )
    aggregate = metrics.get("aggregate") or {}
    if aggregate:
        rows.append(
            _row(
                benchmark=name,
                run="AGGREGATE",
                defense=defense,
                num_cases=aggregate.get("total"),
                utility=None,
                asr=_frac(aggregate.get("micro_asr_percent")),
                score=_frac(aggregate.get("macro_asr_percent")),
                refuse_rate=None,
                status=info.get("status"),
            )
        )
    if not rows:
        rows.append(
            _row(
                benchmark=name, run="all", defense=defense,
                num_cases=info.get("num_cases"),
                utility=None, asr=None, score=None, refuse_rate=None,
                status=info.get("status"),
            )
        )
    return rows


_EXTRACTORS: dict[str, Callable[[str, dict, str], list[dict]]] = {
    "agentharm": _agentharm_rows,
    "agentdojo": _agentdojo_rows,
    "asb": _asb_rows,
    "lps_bench": _lps_bench_rows,
    "agent_safetybench": _agent_safetybench_rows,
    "agentlab": _agentlab_rows,
}


def _default_rows(name: str, info: dict, defense: str) -> list[dict]:
    m = info.get("metrics") or {}
    return [
        _row(
            benchmark=name, run="all", defense=defense,
            num_cases=info.get("num_cases"),
            utility=None, asr=None, score=None, refuse_rate=None,
            status=info.get("status") or m.get("status"),
        )
    ]


# ----------------------------------------------------------- public surface
def build_rows(summary: dict[str, Any], defense: str) -> list[dict]:
    """Flatten ``summary["benchmarks"]`` into one row per benchmark/run pair."""
    rows: list[dict] = []
    for bench_name, bench_info in (summary.get("benchmarks") or {}).items():
        extractor = _EXTRACTORS.get(bench_name, _default_rows)
        rows.extend(extractor(bench_name, bench_info or {}, defense))
    return rows


def render_table(rows: list[dict]) -> str:
    """Format rows as a simple monospace table (no external deps)."""
    if not rows:
        return "(no benchmark results)\n"

    formatted: list[dict[str, str]] = []
    for r in rows:
        formatted.append(
            {
                "benchmark": str(r.get("benchmark", "")),
                "run": str(r.get("run", "")),
                "defense": str(r.get("defense", "")),
                "num_cases": _fmt_int(r.get("num_cases")),
                "utility": _fmt_num(r.get("utility")),
                "ASR": _fmt_num(r.get("ASR")),
                "score": _fmt_num(r.get("score")),
                "refuse_rate": _fmt_num(r.get("refuse_rate")),
                "status": str(r.get("status", "")),
            }
        )
    widths = {col: max(len(col), max(len(row[col]) for row in formatted)) for col in COLUMNS}
    sep = "  "
    lines: list[str] = []
    lines.append(sep.join(col.ljust(widths[col]) for col in COLUMNS))
    lines.append(sep.join("-" * widths[col] for col in COLUMNS))
    for row in formatted:
        lines.append(sep.join(row[col].ljust(widths[col]) for col in COLUMNS))
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict], path: Path) -> None:
    """Write rows to ``path`` using the same column order as the printed table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({col: r.get(col) for col in COLUMNS})
