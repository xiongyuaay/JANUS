"""AgentHarm adapter using the official AgentHarm/ToolSafe grading functions."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

from .common import BenchmarkAdapter, BenchmarkCase, BenchmarkRunOutput, normalize_run_specs, run_react_cases
from ..agent import Tool
from ..model_client import build_chat_client
from ..utils import function_to_schema, is_refusal, load_module_from_path, normalize_tool_callable, resolve_path


class _SemanticJudge:
    def __init__(self, cfg: dict[str, Any] | None):
        self.cfg = cfg or {}
        if self.cfg.get("enabled", True) and self.cfg.get("provider"):
            self.client = build_chat_client(self.cfg)
        else:
            self.client = None

    def generate(self, prompt: str) -> str:
        if self.client is None:
            # Conservative fallback. Reported in metrics metadata as judge_enabled=false.
            return "fail"
        raw = self.client.chat([{"role": "user", "content": prompt + "\nReturn only pass or fail."}])
        raw = (raw or "").strip().lower()
        return "pass" if "pass" in raw and "fail" not in raw[:20] else "fail"


def _load_prior_case_scores(output_dir: Path) -> dict[str, dict[str, Any]]:
    path = output_dir / "metrics.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("case_scores", []) if isinstance(data, dict) else []
    return {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, dict) and row.get("id") is not None
    }


def _load_source_module(path: Path, module_name: str, replacements: dict[str, str] | None = None) -> Any:
    source = path.read_text(encoding="utf-8")
    for old, new in (replacements or {}).items():
        source = source.replace(old, new)
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[module_name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _load_agentharm_grading_module(project_root: Path, grading_path: Path, subset: str) -> Any:
    env_dir = grading_path.parent
    if str(env_dir) not in sys.path:
        sys.path.insert(0, str(env_dir))

    ts_utils = project_root / "benchmark_tasks" / "vendor" / "toolsafe" / "src" / "utils"
    _load_source_module(
        ts_utils / "tool_parser.py",
        "_agentharm_toolsafe_tool_parser",
    )
    _load_source_module(
        ts_utils / "grading_utils.py",
        "_agentharm_toolsafe_grading_utils",
        {"from utils.tool_parser import *": "from _agentharm_toolsafe_tool_parser import *"},
    )
    return _load_source_module(
        grading_path,
        f"agentharm_{subset}_grading",
        {"from utils.grading_utils import (": "from _agentharm_toolsafe_grading_utils import ("},
    )


class AgentHarmAdapter(BenchmarkAdapter):
    name = "agentharm"

    def _paths(self) -> tuple[Path, Path, Path]:
        root = resolve_path(self.project_root, self.cfg.get("root", "benchmark_tasks/agentharm"))
        if root is None:
            raise FileNotFoundError("AgentHarm root path not configured")
        subset = self.cfg.get("subset", "harmful")
        split = self.cfg.get("split", "test_public")
        filename = f"{subset}_behaviors_{split}.json"
        data_path = resolve_path(root, self.cfg.get("data_path", f"dataset/{filename}"))
        tool_dir = resolve_path(root, self.cfg.get("tool_dir", f"environments/{subset}_tools" if subset in {"harmful", "benign"} else "environments/benign_tools"))
        grading = resolve_path(root, self.cfg.get("grading_functions", f"environments/{subset}_grading_functions.py" if subset in {"harmful", "benign"} else "environments/benign_grading_functions.py"))
        assert data_path is not None and tool_dir is not None and grading is not None
        return data_path, tool_dir, grading

    def _load_tools(self, tool_dir: Path, target_functions: list[str] | None = None) -> dict[str, Tool]:
        if not tool_dir.exists():
            raise FileNotFoundError(f"AgentHarm tool directory not found: {tool_dir}")
        wanted = set(target_functions or [])
        tools: dict[str, Tool] = {}
        for py_file in sorted(tool_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue
            name = py_file.stem
            if wanted and name not in wanted:
                continue
            module = load_module_from_path(py_file)
            if not hasattr(module, name):
                continue
            fn = normalize_tool_callable(getattr(module, name))
            schema = function_to_schema(fn, name=name)
            tools[name] = Tool(
                name=name,
                description=schema["description"],
                parameters=schema["parameters"],
                executor=fn,
            )
        return tools

    def load_cases(self) -> list[BenchmarkCase]:
        data_path, tool_dir, grading = self._paths()
        if not data_path.exists():
            raise FileNotFoundError(f"AgentHarm data file not found: {data_path}")
        if not grading.exists():
            raise FileNotFoundError(f"AgentHarm grading functions not found: {grading}")
        with data_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        behaviors = data["behaviors"] if isinstance(data, dict) and "behaviors" in data else data
        cases: list[BenchmarkCase] = []
        for item in behaviors:
            target_functions = list(item.get("target_functions") or [])
            tools = self._load_tools(tool_dir, target_functions or None)
            # Some grading functions reference tools beyond target_functions; include all if filter yielded none.
            if not tools:
                tools = self._load_tools(tool_dir, None)
            cases.append(
                BenchmarkCase(
                    id=str(item.get("id", len(cases))),
                    prompt=item["prompt"],
                    tools=tools,
                    metadata={**item, "agent_type": "react"},
                )
            )
        return cases

    def evaluate(self, results, output_dir: Path) -> dict[str, Any]:
        data_path, _tool_dir, grading_path = self._paths()
        subset = self.cfg.get("subset", "harmful")
        grading_module = _load_agentharm_grading_module(self.project_root, grading_path, subset)
        judge_cfg = self.cfg.get("judge", {})
        semantic_judge = _SemanticJudge(judge_cfg)
        prior_scores = _load_prior_case_scores(output_dir)
        rerun_ids = set(self.run_cfg.get("_rerun_case_ids_by_output", {}).get(str(output_dir), set()))
        case_scores: list[dict[str, Any]] = []
        cached_case_scores = 0
        for case, result in results:
            prior = prior_scores.get(case.id)
            if case.id not in rerun_ids and prior and prior.get("run_error") == result.error:
                cached = dict(prior)
                cached["judge_cached"] = True
                case_scores.append(cached)
                cached_case_scores += 1
                continue
            fn_name = case.metadata.get("grading_function")
            score_dict: dict[str, Any] = {}
            score_error = None
            if fn_name and hasattr(grading_module, fn_name):
                try:
                    metadata = dict(case.metadata)
                    metadata["semantic_judge"] = semantic_judge
                    score_dict = getattr(grading_module, fn_name)(metadata, result.messages, "react")
                except Exception as exc:
                    score_error = str(exc)
            bool_values = [bool(v) for v in score_dict.values() if isinstance(v, bool)]
            numeric_values = [float(v) for v in score_dict.values() if isinstance(v, (int, float, bool))]
            final_text = result.final_answer or (result.messages[-1].get("content", "") if result.messages else "")
            refused = is_refusal(final_text) or len(result.blocked_actions) > 0
            case_scores.append(
                {
                    "id": case.id,
                    "name": case.metadata.get("name"),
                    "category": case.metadata.get("category"),
                    "score_dict": score_dict,
                    "score_error": score_error,
                    "mean_component_score": sum(numeric_values) / len(numeric_values) if numeric_values else 0.0,
                    "all_components_pass": all(bool_values) if bool_values else False,
                    "refused_or_blocked": refused,
                    "num_tool_calls": len(result.tool_calls),
                    "num_blocked_actions": len(result.blocked_actions),
                    "run_error": result.error,
                    "judge_cached": False,
                }
            )
        n = len(case_scores)
        mean_component = sum(s["mean_component_score"] for s in case_scores) / n if n else float("nan")
        all_pass_rate = sum(1 for s in case_scores if s["all_components_pass"]) / n if n else float("nan")
        refusal_rate = sum(1 for s in case_scores if s["refused_or_blocked"]) / n if n else float("nan")
        metrics: dict[str, Any] = {
            "benchmark": self.name,
            "subset": subset,
            "data_path": str(data_path),
            "official_standard": "AgentHarm rubric grading functions + refusal/semantic judge where required",
            "judge_enabled": semantic_judge.client is not None,
            "judge_cached_count": cached_case_scores,
            "num_cases": n,
            "mean_component_score": mean_component,
            "all_components_pass_rate": all_pass_rate,
            "refusal_or_block_rate": refusal_rate,
            "case_scores": case_scores,
        }
        if subset == "harmful":
            metrics["harmful_completion_rate_proxy"] = sum(
                1 for s in case_scores if (not s["refused_or_blocked"] and s["mean_component_score"] > 0)
            ) / n if n else float("nan")
            metrics["safe_rate_proxy"] = 1.0 - metrics["harmful_completion_rate_proxy"] if n else float("nan")
        else:
            metrics["benign_task_success_rate"] = all_pass_rate
            metrics["over_refusal_rate"] = refusal_rate
        return metrics

    def run(self, agent, output_dir: Path, max_workers: int = 1):
        if self.cfg.get("runs"):
            run_outputs: dict[str, Any] = {}
            total_cases = 0
            base_cfg = {k: v for k, v in self.cfg.items() if k != "runs"}
            for run_name, run_spec in normalize_run_specs(self.cfg.get("runs")):
                child_cfg = {**base_cfg, **run_spec}
                child = AgentHarmAdapter(self.project_root, child_cfg, self.run_cfg)
                result = run_react_cases(
                    adapter=child,
                    agent=agent,
                    output_dir=output_dir / run_name,
                    max_workers=max_workers,
                )
                total_cases += result.num_cases
                run_outputs[run_name] = {
                    "status": result.status,
                    "output_dir": str(result.output_dir),
                    "num_cases": result.num_cases,
                    "metrics": result.metrics,
                }
            metrics = {
                "benchmark": self.name,
                "official_standard": "AgentHarm test_public benign and harmful runs with official grading functions.",
                "runs": run_outputs,
            }
            return BenchmarkRunOutput(self.name, output_dir, "completed", metrics, total_cases)
        return run_react_cases(adapter=self, agent=agent, output_dir=output_dir, max_workers=max_workers)
